/* Ed25519 signing (RFC 8032) + SHA-512 — BigInt, dependency-free.
 *
 * Written for the server-served JouleX UI so a browser can sign
 * CF-KYA presentations with the demo agent's key and place a paper
 * order through the live API — no admission bypass, the same
 * verifier checks the signature server-side.
 *
 * SHA-512 K constants derived independently in Python (Decimal
 * cube-root method, 60-digit precision) and verified by testing
 * sha512() against Node's crypto on fixed inputs; Ed25519 verified
 * against RFC 8032 §7.1 TEST 1 + TEST 2 and against Python's
 * ed25519 (see tests/webui_crypto.cjs).
 *
 * Signing only — no verification, no key generation. The seed never
 * leaves the page's memory (demo agents only; the UI never stores
 * it). UMD: browser global window.JoulexEd, Node module.exports.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.JoulexEd = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MASK64 = (1n << 64n) - 1n;

  // -- SHA-512 (FIPS 180-4) -------------------------------------------
  var H_INIT = [
    0x6a09e667f3bcc908n, 0xbb67ae8584caa73bn,
    0x3c6ef372fe94f82bn, 0xa54ff53a5f1d36f1n,
    0x510e527fade682d1n, 0x9b05688c2b3e6c1fn,
    0x1f83d9abfb41bd6bn, 0x5be0cd19137e2179n,
  ];
  var K = [
    0x428a2f98d728ae22n, 0x7137449123ef65cdn, 0xb5c0fbcfec4d3b2fn, 0xe9b5dba58189dbbcn,
    0x3956c25bf348b538n, 0x59f111f1b605d019n, 0x923f82a4af194f9bn, 0xab1c5ed5da6d8118n,
    0xd807aa98a3030242n, 0x12835b0145706fben, 0x243185be4ee4b28cn, 0x550c7dc3d5ffb4e2n,
    0x72be5d74f27b896fn, 0x80deb1fe3b1696b1n, 0x9bdc06a725c71235n, 0xc19bf174cf692694n,
    0xe49b69c19ef14ad2n, 0xefbe4786384f25e3n, 0x0fc19dc68b8cd5b5n, 0x240ca1cc77ac9c65n,
    0x2de92c6f592b0275n, 0x4a7484aa6ea6e483n, 0x5cb0a9dcbd41fbd4n, 0x76f988da831153b5n,
    0x983e5152ee66dfabn, 0xa831c66d2db43210n, 0xb00327c898fb213fn, 0xbf597fc7beef0ee4n,
    0xc6e00bf33da88fc2n, 0xd5a79147930aa725n, 0x06ca6351e003826fn, 0x142929670a0e6e70n,
    0x27b70a8546d22ffcn, 0x2e1b21385c26c926n, 0x4d2c6dfc5ac42aedn, 0x53380d139d95b3dfn,
    0x650a73548baf63den, 0x766a0abb3c77b2a8n, 0x81c2c92e47edaee6n, 0x92722c851482353bn,
    0xa2bfe8a14cf10364n, 0xa81a664bbc423001n, 0xc24b8b70d0f89791n, 0xc76c51a30654be30n,
    0xd192e819d6ef5218n, 0xd69906245565a910n, 0xf40e35855771202an, 0x106aa07032bbd1b8n,
    0x19a4c116b8d2d0c8n, 0x1e376c085141ab53n, 0x2748774cdf8eeb99n, 0x34b0bcb5e19b48a8n,
    0x391c0cb3c5c95a63n, 0x4ed8aa4ae3418acbn, 0x5b9cca4f7763e373n, 0x682e6ff3d6b2b8a3n,
    0x748f82ee5defb2fcn, 0x78a5636f43172f60n, 0x84c87814a1f0ab72n, 0x8cc702081a6439ecn,
    0x90befffa23631e28n, 0xa4506cebde82bde9n, 0xbef9a3f7b2c67915n, 0xc67178f2e372532bn,
    0xca273eceea26619cn, 0xd186b8c721c0c207n, 0xeada7dd6cde0eb1en, 0xf57d4f7fee6ed178n,
    0x06f067aa72176fban, 0x0a637dc5a2c898a6n, 0x113f9804bef90daen, 0x1b710b35131c471bn,
    0x28db77f523047d84n, 0x32caab7b40c72493n, 0x3c9ebe0a15c9bebcn, 0x431d67c49c100d4cn,
    0x4cc5d4becb3e42b6n, 0x597f299cfc657e2an, 0x5fcb6fab3ad6faecn, 0x6c44198c4a475817n,
  ];

  function rotr64(x, n) {
    return ((x >> BigInt(n)) | (x << BigInt(64 - n))) & MASK64;
  }

  function sha512(msg) {
    // msg: Uint8Array. Returns Uint8Array(64).
    var bitLen = BigInt(msg.length) * 8n;
    var paddedLen = msg.length + 1;
    while (paddedLen % 128 !== 112) paddedLen++;
    paddedLen += 16;
    var padded = new Uint8Array(paddedLen);
    padded.set(msg, 0);
    padded[msg.length] = 0x80;
    // 128-bit big-endian bit length; high 64 bits are zero for any
    // message this UI can build (<< 2^61 bytes).
    for (var i = 0; i < 8; i++) {
      padded[paddedLen - 1 - i] = Number((bitLen >> BigInt(8 * i)) & 0xffn);
    }
    var h = H_INIT.slice();
    var w = new Array(80);
    for (var off = 0; off < paddedLen; off += 128) {
      for (var t = 0; t < 16; t++) {
        var v = 0n;
        for (var b = 0; b < 8; b++) v = (v << 8n) | BigInt(padded[off + t * 8 + b]);
        w[t] = v;
      }
      for (t = 16; t < 80; t++) {
        var s0 = rotr64(w[t - 15], 1) ^ rotr64(w[t - 15], 8) ^ (w[t - 15] >> 7n);
        var s1 = rotr64(w[t - 2], 19) ^ rotr64(w[t - 2], 61) ^ (w[t - 2] >> 6n);
        w[t] = (s1 + w[t - 7] + s0 + w[t - 16]) & MASK64;
      }
      var a = h[0], bb = h[1], c = h[2], d = h[3];
      var e = h[4], f = h[5], g = h[6], hh = h[7];
      for (t = 0; t < 80; t++) {
        var S1 = rotr64(e, 14) ^ rotr64(e, 18) ^ rotr64(e, 41);
        var ch = (e & f) ^ (~e & g);
        var temp1 = (hh + S1 + ch + K[t] + w[t]) & MASK64;
        var S0 = rotr64(a, 28) ^ rotr64(a, 34) ^ rotr64(a, 39);
        var maj = (a & bb) ^ (a & c) ^ (bb & c);
        var temp2 = (S0 + maj) & MASK64;
        hh = g; g = f; f = e; e = (d + temp1) & MASK64;
        d = c; c = bb; bb = a; a = (temp1 + temp2) & MASK64;
      }
      h[0] = (h[0] + a) & MASK64; h[1] = (h[1] + bb) & MASK64;
      h[2] = (h[2] + c) & MASK64; h[3] = (h[3] + d) & MASK64;
      h[4] = (h[4] + e) & MASK64; h[5] = (h[5] + f) & MASK64;
      h[6] = (h[6] + g) & MASK64; h[7] = (h[7] + hh) & MASK64;
    }
    var out = new Uint8Array(64);
    for (var i2 = 0; i2 < 8; i2++) {
      for (var j = 0; j < 8; j++) out[i2 * 8 + j] = Number((h[i2] >> BigInt(56 - 8 * j)) & 0xffn);
    }
    return out;
  }

  // -- Ed25519 (RFC 8032) ----------------------------------------------
  var P = (1n << 255n) - 19n;
  var L = (1n << 252n) + 27742317777372353535851937790883648493n;

  function mod(a) {
    var r = a % P;
    return r < 0n ? r + P : r;
  }
  function modPow(b, e) {
    var r = 1n;
    b = mod(b);
    while (e > 0n) {
      if (e & 1n) r = mod(r * b);
      b = mod(b * b);
      e >>= 1n;
    }
    return r;
  }
  function modInv(a) {
    // Extended Euclidean algorithm.
    var oldR = mod(a), r = P;
    var oldS = 1n, s = 0n;
    while (r !== 0n) {
      var q = oldR / r;
      var tmp = oldR - q * r; oldR = r; r = tmp;
      tmp = oldS - q * s; oldS = s; s = tmp;
    }
    return mod(oldS);
  }
  var D = mod(-121665n * modInv(121666n));

  function sqrtMod(u) {
    // p = 5 mod 8: x = u^((p+3)/8); if x^2 != u, x *= 2^((p-1)/4).
    var x = modPow(u, (P + 3n) / 8n);
    if (mod(x * x) !== mod(u)) x = mod(x * modPow(2n, (P - 1n) / 4n));
    if (mod(x * x) !== mod(u)) throw new Error("Ed25519: no square root");
    return x;
  }

  // Base point: y = 4/5, x even (compressed sign bit 0).
  var BY = mod(4n * modInv(5n));
  var BX = (function () {
    var x = sqrtMod(mod((BY * BY - 1n) * modInv(mod(D * BY * BY + 1n))));
    return (x & 1n) ? P - x : x;
  })();
  // Extended coordinates (X, Y, T, Z), a = -1.
  var BASE = [BX, BY, mod(BX * BY), 1n];
  var IDENTITY = [0n, 1n, 0n, 1n];

  function pointAdd(p1, p2) {
    var A = mod((p1[1] - p1[0]) * (p2[1] - p2[0]));
    var B = mod((p1[1] + p1[0]) * (p2[1] + p2[0]));
    var C = mod(p1[2] * 2n * D * p2[2]);
    var Dd = mod(p1[3] * 2n * p2[3]);
    var E = mod(B - A);
    var F = mod(Dd - C);
    var G = mod(Dd + C);
    var H = mod(B + A);
    return [mod(E * F), mod(G * H), mod(E * H), mod(F * G)];
  }
  function pointDouble(p) {
    var A = mod(p[0] * p[0]);
    var B = mod(p[1] * p[1]);
    var C = mod(2n * p[3] * p[3]);
    var Dd = mod(-A);
    var E = mod(mod((p[0] + p[1]) * (p[0] + p[1])) - A - B);
    var G = mod(Dd + B);
    var F = mod(G - C);
    var H = mod(Dd - B);
    return [mod(E * F), mod(G * H), mod(E * H), mod(F * G)];
  }
  function scalarMult(s, point) {
    var q = IDENTITY;
    for (var i = 254; i >= 0; i--) {
      q = pointDouble(q);
      if ((s >> BigInt(i)) & 1n) q = pointAdd(q, point);
    }
    return q;
  }
  function encodePoint(p) {
    var x = mod(p[0] * modInv(p[3]));
    var y = mod(p[1] * modInv(p[3]));
    var out = new Uint8Array(32);
    var yy = y;
    for (var i = 0; i < 32; i++) {
      out[i] = Number(yy & 0xffn);
      yy >>= 8n;
    }
    if (x & 1n) out[31] |= 0x80;
    return out;
  }
  function leToBig(bytes) {
    var v = 0n;
    for (var i = bytes.length - 1; i >= 0; i--) v = (v << 8n) | BigInt(bytes[i]);
    return v;
  }
  function clampScalar(h32) {
    var b = new Uint8Array(h32);
    b[0] &= 248; b[31] &= 127; b[31] |= 64;
    return leToBig(b);
  }
  function concatBytes(parts) {
    var len = 0;
    for (var i = 0; i < parts.length; i++) len += parts[i].length;
    var out = new Uint8Array(len);
    var off = 0;
    for (var j = 0; j < parts.length; j++) {
      out.set(parts[j], off);
      off += parts[j].length;
    }
    return out;
  }

  function publicKeyFromSeed(seed) {
    var h = sha512(seed);
    return encodePoint(scalarMult(clampScalar(h.slice(0, 32)), BASE));
  }

  function sign(message, seed, publicKey) {
    var h = sha512(seed);
    var a = clampScalar(h.slice(0, 32));
    var prefix = h.slice(32, 64);
    var r = leToBig(sha512(concatBytes([prefix, message]))) % L;
    var R = encodePoint(scalarMult(r, BASE));
    var k = leToBig(sha512(concatBytes([R, publicKey, message]))) % L;
    var S = (r + k * a) % L;
    var out = new Uint8Array(64);
    out.set(R, 0);
    var s = S;
    for (var i = 0; i < 32; i++) {
      out[32 + i] = Number(s & 0xffn);
      s >>= 8n;
    }
    return out;
  }

  return { sha512: sha512, publicKeyFromSeed: publicKeyFromSeed, sign: sign };
});
