// hcs_transfer.mjs — transfer testnet HBAR from the operator to one account.
//
// Called by joulex/transfer.py (TransferClient) only. The operator key
// comes from the environment (JOULEX_HCS_PRIVATE_KEY) — never argv,
// never logged, never written to disk by this script.
//
// TESTNET ONLY. There is no mainnet code path: any network other than
// "testnet" fails immediately, before any key is read. This leg moves
// worthless test tokens only; it is the value rail for paper
// settlements, never real money.
//
// Usage:
//   node hcs_transfer.mjs <to_account> <amount_tinybars> <memo> <network>
// Prints exactly one JSON receipt line on stdout:
//   {"status":"SUCCESS","transaction_id":"...","consensus_timestamp":"...","amount_tinybars":N,"to_account":"..."}
// Exits non-zero with {"error": "..."} on stderr on failure.
//
// LIVE RUN (owner only — needs the operator key supplied transiently):
//   JOULEX_HCS_OPERATOR=0.0.<operator> JOULEX_HCS_PRIVATE_KEY=<ecdsa-key> \
//     node joulex/hcs_transfer.mjs <to_account> <amount_tinybars> <settlement_id> testnet
// Then record it against the settlement with transfer.record_transfer()
// (or let the coordinator call update_receipt_transfer() to fill the
// settlement receipt's transfer_tx_id). Never put the key in a file,
// a shell history entry, or argv.

import { Client, Hbar, PrivateKey, TransferTransaction } from "@hashgraph/sdk";

const [toAccount, amountRaw, memo, network] = process.argv.slice(2);
const operatorId = process.env.JOULEX_HCS_OPERATOR;
const keyStr = process.env.JOULEX_HCS_PRIVATE_KEY;

function fail(msg) {
    process.stderr.write(JSON.stringify({ error: String(msg) }) + "\n");
    process.exit(1);
}

if (!toAccount || amountRaw === undefined || memo === undefined || !network) {
    fail("usage: hcs_transfer.mjs <to_account> <amount_tinybars> <memo> <network>");
}
// Fail closed, before touching any key: testnet only, no mainnet path.
if (network !== "testnet") fail(`unsupported network: ${network} (testnet only)`);
if (!/^0\.0\.\d+$/.test(toAccount)) fail(`bad to_account: ${toAccount}`);
if (!/^\d+$/.test(amountRaw)) fail(`amount_tinybars must be a positive integer: ${amountRaw}`);
const amountTinybars = Number(amountRaw);
if (!Number.isSafeInteger(amountTinybars) || amountTinybars <= 0) {
    fail(`amount_tinybars must be a positive integer: ${amountRaw}`);
}
if (memo.length === 0 || memo.length > 100) fail("memo must be 1-100 characters");
if (!operatorId) fail("JOULEX_HCS_OPERATOR is not set");
if (!keyStr) fail("JOULEX_HCS_PRIVATE_KEY is not set");

let client;
try {
    const key = PrivateKey.fromStringECDSA(keyStr);
    client = Client.forTestnet().setOperator(operatorId, key);
    const tx = await new TransferTransaction()
        .addHbarTransfer(operatorId, Hbar.fromTinybars(-amountTinybars))
        .addHbarTransfer(toAccount, Hbar.fromTinybars(amountTinybars))
        .setTransactionMemo(memo)
        .execute(client);
    const receipt = await tx.getReceipt(client);
    let consensusTimestamp = null;
    try {
        const record = await tx.getRecord(client);
        consensusTimestamp = record.consensusTimestamp ? record.consensusTimestamp.toString() : null;
    } catch {
        // Receipt is authoritative; the consensus timestamp is also
        // readable from the mirror node after the fact.
    }
    process.stdout.write(JSON.stringify({
        status: String(receipt.status),
        transaction_id: String(tx.transactionId),
        consensus_timestamp: consensusTimestamp,
        amount_tinybars: amountTinybars,
        to_account: toAccount,
    }) + "\n");
    process.exit(0);
} catch (err) {
    fail(err && err.message ? err.message : err);
} finally {
    if (client) client.close();
}
