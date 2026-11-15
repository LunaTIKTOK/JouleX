/* LMSR port — line-for-line from joulex/lmsr.py (M6 generalized rule).
 *
 * The binary market is the N=2 case of the same functions. All point
 * amounts are rounded to 6 decimal places at the trade boundary, so a
 * buy-then-sell round trip returns the cost exactly — same rule as
 * Python. Rounding is Python round-half-even (banker's), not JS
 * Math.round, so costs match the Python module bit-for-bit.
 *
 * UMD: works as a browser global (window.JoulexLMSR) and as a Node
 * module (module.exports). No DOM, no network.
 */
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  } else {
    root.JoulexLMSR = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var DEFAULT_B = 100.0;
  var MONEY_DP = 6;

  // Python round(x, dp): ties to even. Mirrors CPython for the finite
  // doubles this module ever sees.
  function pyRound(value, dp) {
    var p = Math.pow(10, dp);
    var n = value * p;
    var f = Math.floor(n);
    var diff = n - f;
    if (diff > 0.5) return (f + 1) / p;
    if (diff < 0.5) return f / p;
    // Exact tie: round to even.
    return (f % 2 === 0 ? f : f + 1) / p;
  }

  function roundMoney(value) {
    return pyRound(value, MONEY_DP);
  }

  function costFunctionMulti(q, b) {
    if (b === undefined) b = DEFAULT_B;
    if (b <= 0) throw new Error("liquidity parameter b must be > 0");
    if (q.length < 2) throw new Error("multi-outcome LMSR needs at least 2 outcomes");
    var m = -Infinity;
    var i;
    for (i = 0; i < q.length; i++) m = Math.max(m, q[i] / b);
    var s = 0;
    for (i = 0; i < q.length; i++) s += Math.exp(q[i] / b - m);
    return b * (m + Math.log(s));
  }

  function pricesMulti(q, b) {
    if (b === undefined) b = DEFAULT_B;
    if (b <= 0) throw new Error("liquidity parameter b must be > 0");
    if (q.length < 2) throw new Error("multi-outcome LMSR needs at least 2 outcomes");
    var m = -Infinity;
    var i;
    for (i = 0; i < q.length; i++) m = Math.max(m, q[i] / b);
    var weights = [];
    var total = 0;
    for (i = 0; i < q.length; i++) {
      var w = Math.exp(q[i] / b - m);
      weights.push(w);
      total += w;
    }
    return weights.map(function (w) { return w / total; });
  }

  function applyTradeMulti(q, index, shares) {
    if (!(index >= 0 && index < q.length)) {
      throw new Error("outcome index " + index + " out of range for " + q.length + " outcomes");
    }
    var out = q.slice();
    out[index] = out[index] + shares;
    return out;
  }

  function tradeCostMulti(q, b, index, shares) {
    if (!(index >= 0 && index < q.length)) {
      throw new Error("outcome index " + index + " out of range for " + q.length + " outcomes");
    }
    var newQ = applyTradeMulti(q, index, shares);
    return roundMoney(costFunctionMulti(newQ, b) - costFunctionMulti(q, b));
  }

  // Binary specializations (exact, same arithmetic as the multi path).
  function costFunction(qYes, qNo, b) {
    return costFunctionMulti([qYes, qNo], b === undefined ? DEFAULT_B : b);
  }

  function priceYes(qYes, qNo, b) {
    return pricesMulti([qYes, qNo], b === undefined ? DEFAULT_B : b)[0];
  }

  function tradeCost(qYes, qNo, b, side, shares) {
    var idx = side === "YES" ? 0 : side === "NO" ? 1 : -1;
    if (idx < 0) throw new Error("side must be YES or NO, got " + JSON.stringify(side));
    return tradeCostMulti([qYes, qNo], b, idx, shares);
  }

  return {
    DEFAULT_B: DEFAULT_B,
    MONEY_DP: MONEY_DP,
    pyRound: pyRound,
    roundMoney: roundMoney,
    costFunctionMulti: costFunctionMulti,
    pricesMulti: pricesMulti,
    applyTradeMulti: applyTradeMulti,
    tradeCostMulti: tradeCostMulti,
    costFunction: costFunction,
    priceYes: priceYes,
    tradeCost: tradeCost
  };
});
