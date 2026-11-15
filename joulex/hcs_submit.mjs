// hcs_submit.mjs — submit one message to a Hedera HCS topic.
//
// Called by joulex/hcs.py (RealHCS) only. The operator key comes from
// the environment (JOULEX_HCS_PRIVATE_KEY) — never argv, never logged,
// never written to disk by this script.
//
// Usage:
//   node hcs_submit.mjs <topic_id> <network> <message>
// Prints one JSON receipt line on stdout:
//   {"status":"SUCCESS","topic_sequence":"12","consensus_timestamp":"...","transaction_id":"..."}
// Exits non-zero with {"error": "..."} on stderr on failure.

import { Client, PrivateKey, TopicMessageSubmitTransaction } from "@hashgraph/sdk";

const [topicId, network, message] = process.argv.slice(2);
const operatorId = process.env.JOULEX_HCS_OPERATOR;
const keyStr = process.env.JOULEX_HCS_PRIVATE_KEY;

function fail(msg) {
    process.stderr.write(JSON.stringify({ error: String(msg) }) + "\n");
    process.exit(1);
}

if (!topicId || !network || message === undefined) fail("usage: hcs_submit.mjs <topic_id> <network> <message>");
if (!operatorId) fail("JOULEX_HCS_OPERATOR is not set");
if (!keyStr) fail("JOULEX_HCS_PRIVATE_KEY is not set");
if (network !== "testnet" && network !== "mainnet") fail(`unsupported network: ${network}`);

let client;
try {
    const key = PrivateKey.fromStringECDSA(keyStr);
    client = (network === "testnet" ? Client.forTestnet() : Client.forMainnet()).setOperator(operatorId, key);
    const tx = await new TopicMessageSubmitTransaction()
        .setTopicId(topicId)
        .setMessage(message)
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
    const seq = receipt.topicSequenceNumber;
    process.stdout.write(JSON.stringify({
        status: String(receipt.status),
        topic_sequence: seq === null || seq === undefined ? null : String(seq),
        consensus_timestamp: consensusTimestamp,
        transaction_id: String(tx.transactionId),
    }) + "\n");
    process.exit(0);
} catch (err) {
    fail(err && err.message ? err.message : err);
} finally {
    if (client) client.close();
}
