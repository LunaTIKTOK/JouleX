/* Synchronous SHA-256 + JCS-style canonical JSON.
 *
 * canonical_bytes in joulex/attestation.py is json.dumps(sort_keys=True,
 * separators=(",", ":")) — this file mirrors it exactly, including
 * Python float formatting (repr shortest-round-trip; scientific
 * notation below 1e-4 / at or above 1e16, exponent zero-padded to two
 * digits), so payload hashes computed here match the Python module's
 * for the same payload. Verified against Python in tests/parity.mjs.
 *
 * The SHA-256 below is a standard compact implementation (FIPS 180-4);
 * verified against Node's crypto in tests/parity.mjs. It is synchronous
 * so the audit log can append in one call in both the browser and Node.
 *
 * UMD: browser global window.JoulexHash, Node module.exports.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.JoulexHash = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // -- SHA-256 (FIPS 180-4) -------------------------------------------
  var K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }

  function sha256Hex(str) {
    // UTF-8 encode.
    var bytes = [];
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      if (c < 0x80) bytes.push(c);
      else if (c < 0x800) { bytes.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f)); }
      else if (c >= 0xd800 && c < 0xdc00) {
        var c2 = str.charCodeAt(++i);
        var cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
        bytes.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f),
                   0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      } else {
        bytes.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f));
      }
    }
    var bitLen = bytes.length * 8;
    bytes.push(0x80);
    while (bytes.length % 64 !== 56) bytes.push(0);
    // 64-bit length, big-endian (message is far below 2^32 bits).
    bytes.push(0, 0, 0, 0, (bitLen >>> 24) & 0xff, (bitLen >>> 16) & 0xff,
               (bitLen >>> 8) & 0xff, bitLen & 0xff);

    var h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
             0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var w = new Array(64);
    for (var off = 0; off < bytes.length; off += 64) {
      for (var t = 0; t < 16; t++) {
        w[t] = ((bytes[off + t * 4] << 24) | (bytes[off + t * 4 + 1] << 16) |
                (bytes[off + t * 4 + 2] << 8) | bytes[off + t * 4 + 3]) >>> 0;
      }
      for (t = 16; t < 64; t++) {
        var s0 = (rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3)) >>> 0;
        var s1 = (rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10)) >>> 0;
        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
      }
      var a = h[0], b = h[1], c2 = h[2], d = h[3];
      var e = h[4], f = h[5], g = h[6], hh = h[7];
      for (t = 0; t < 64; t++) {
        var S1 = (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) >>> 0;
        var ch = ((e & f) ^ (~e & g)) >>> 0;
        var t1 = (hh + S1 + ch + K[t] + w[t]) >>> 0;
        var S0 = (rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) >>> 0;
        var maj = ((a & b) ^ (a & c2) ^ (b & c2)) >>> 0;
        var t2 = (S0 + maj) >>> 0;
        hh = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c2; c2 = b; b = a; a = (t1 + t2) >>> 0;
      }
      h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0;
      h[2] = (h[2] + c2) >>> 0; h[3] = (h[3] + d) >>> 0;
      h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0;
      h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
    }
    var out = "";
    for (var j = 0; j < 8; j++) {
      out += ("00000000" + (h[j] >>> 0).toString(16)).slice(-8);
    }
    return out;
  }

  // -- Python-compatible float formatting -------------------------------
  // CPython repr(float): shortest string that round-trips; scientific
  // notation when |x| < 1e-4 or >= 1e16, exponent with sign and at
  // least two digits (1e-06, 1e+16). JS String() differs exactly
  // there (0.000001, 1e-6), so translate.
  function pyFloat(x) {
    if (x === 0) return "0.0";
    if (!isFinite(x)) return x > 0 ? "Infinity" : "-Infinity";
    if (Number.isInteger(x) && Math.abs(x) < 1e16) return x.toFixed(1);
    var ax = Math.abs(x);
    if (ax < 1e-4 || ax >= 1e16) {
      var exp = x.toExponential(); // e.g. "1e-6", "1.5e+21"
      var parts = exp.split("e");
      var mant = parts[0];
      var e = parseInt(parts[1], 10);
      var sign = e < 0 ? "-" : "+";
      var digits = String(Math.abs(e));
      if (digits.length < 2) digits = "0" + digits;
      return mant + "e" + sign + digits;
    }
    return String(x);
  }

  // -- JCS-style canonical JSON -----------------------------------------
  // json.dumps(data, sort_keys=True, separators=(",", ":")) with the
  // DEFAULT ensure_ascii=True: every non-ASCII code unit is escaped
  // as \uXXXX (astral characters become surrogate-pair escapes).
  // JSON.stringify leaves non-ASCII raw, so translate after stringify.
  function pyString(s) {
    var out = JSON.stringify(s);
    var res = "";
    for (var i = 0; i < out.length; i++) {
      var code = out.charCodeAt(i);
      if (code > 0x7e) {
        res += "\\u" + ("0000" + code.toString(16)).slice(-4);
      } else {
        res += out[i];
      }
    }
    return res;
  }

  function canonical(data) {
    if (data === null || data === undefined) return "null";
    if (typeof data === "number") return pyFloat(data);
    if (typeof data === "boolean") return data ? "true" : "false";
    if (typeof data === "string") return pyString(data);
    if (Array.isArray(data)) {
      return "[" + data.map(canonical).join(",") + "]";
    }
    var keys = Object.keys(data).sort();
    var parts = [];
    for (var i = 0; i < keys.length; i++) {
      parts.push(pyString(keys[i]) + ":" + canonical(data[keys[i]]));
    }
    return "{" + parts.join(",") + "}";
  }

  function sha256Canonical(data) {
    return sha256Hex(canonical(data));
  }

  return {
    sha256Hex: sha256Hex,
    pyFloat: pyFloat,
    pyString: pyString,
    canonical: canonical,
    sha256Canonical: sha256Canonical
  };
});
