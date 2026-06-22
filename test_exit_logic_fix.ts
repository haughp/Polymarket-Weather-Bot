/**
 * Test: Verify the exit logic no longer auto-closes on API failure
 * 
 * Before fix: 
 *   - getMarketYesPrice returns null → currentPrice = 0
 *   - isExpired && currentPrice <= 0.02 → true → position closes as -$1.05 loss
 * 
 * After fix:
 *   - getMarketYesPrice returns null → currentPrice = 0
 *   - isExpired && rawPrice != null && currentPrice <= 0.02 → false (rawPrice is null)
 *   - Position stays open, no phantom loss recorded
 */

// Simulate the old buggy logic
function oldExitLogic(rawPrice: number | null, isExpired: boolean): string {
  const currentPrice = rawPrice ?? 0;  // ← BUG: Defaults to 0
  
  let resolvedWin: boolean | null = null;
  if (resolvedWin === null && isExpired) {
    // OLD: No check for rawPrice != null
    if (currentPrice >= 0.98) resolvedWin = true;
    else if (currentPrice <= 0.02) resolvedWin = false;
  }
  
  if (resolvedWin === false || (isExpired && currentPrice <= 0.02)) {
    return "CLOSE_LOSS";  // ← Records -$1.05 loss
  }
  return "HOLD";
}

// Simulate the new fixed logic
function newExitLogic(rawPrice: number | null, isExpired: boolean): string {
  const currentPrice = rawPrice ?? 0;
  
  let resolvedWin: boolean | null = null;
  if (resolvedWin === null && isExpired && rawPrice != null) {
    // ← FIX: Check rawPrice != null
    if (currentPrice >= 0.98) resolvedWin = true;
    else if (currentPrice <= 0.02) resolvedWin = false;
  }
  
  if (resolvedWin === false || (isExpired && rawPrice != null && currentPrice <= 0.02)) {
    // ← FIX: Check rawPrice != null
    return "CLOSE_LOSS";
  }
  return "HOLD";
}

// Test case: API returns null (failure), market is expired
const testCases = [
  { rawPrice: null, isExpired: true, description: "API failed, market expired" },
  { rawPrice: 0.01, isExpired: true, description: "API got price $0.01, market expired" },
  { rawPrice: 0.5, isExpired: true, description: "API got price $0.50, market expired" },
  { rawPrice: null, isExpired: false, description: "API failed, market not expired" },
];

console.log("=== Exit Logic Comparison ===\n");
testCases.forEach(({ rawPrice, isExpired, description }) => {
  const oldResult = oldExitLogic(rawPrice, isExpired);
  const newResult = newExitLogic(rawPrice, isExpired);
  const fixed = oldResult !== newResult ? "✅ FIXED" : "";
  console.log(`${description}`);
  console.log(`  rawPrice=${rawPrice}, isExpired=${isExpired}`);
  console.log(`  OLD: ${oldResult}  |  NEW: ${newResult}  ${fixed}`);
  console.log();
});

console.log("=== Summary ===");
console.log("The critical bug case:");
console.log("  When API returns null AND market is expired:");
console.log("  OLD logic: Records -$1.05 loss (phantom, based on API failure)");
console.log("  NEW logic: Holds position (waits for actual resolution)");
