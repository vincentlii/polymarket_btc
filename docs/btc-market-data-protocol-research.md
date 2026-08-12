# BTC Market-Data Protocol Boundaries

> Verified on 2026-07-20. This note uses only first-party exchange
> documentation and first-party source repositories. It defines the
> fail-closed protocol boundary for the BTC forward collector; it does not
> claim behavior that the venues do not document.

## Required collector behavior

| Feed | Synchronization boundary | Gap recovery |
|---|---|---|
| Binance Spot diff depth | WebSocket buffer plus REST snapshot, bridged by `U`/`u` | Discard the local book and repeat snapshot synchronization |
| Binance USD-M Futures diff depth | WebSocket buffer plus REST snapshot, then `pu == previous u` | Discard the local book and repeat snapshot synchronization |
| OKX `books` | Initial `snapshot`, then `prevSeqId`/`seqId` continuity | Invalidate the book and force a fresh subscription snapshot |
| Polymarket CLOB market channel | Full `book` snapshot per token, then `price_change` deltas | Invalidate the token book and resubscribe for a new full `book` |

No feed may expose a usable book before its own synchronization boundary has
passed. A socket being connected is not evidence that its local book is valid.

## 1. Binance Spot diff depth

### Binance Spot official facts

The Spot diff-depth stream provides `U` (first update ID), `u` (final update
ID), and absolute quantities. Binance's official procedure is:

1. Open the WebSocket and buffer updates before requesting the REST snapshot.
2. If snapshot `lastUpdateId` is older than the first buffered `U`, fetch a
   newer snapshot.
3. Drop buffered events whose `u <= lastUpdateId`.
4. Seed the local book from the snapshot, then apply the retained events.
5. Ignore already-covered updates; if an event starts beyond local update ID
   plus one, discard the book and restart synchronization.
6. Set an absolute quantity at each changed price; quantity zero removes the
   level.

These rules and the 5,000-level snapshot limitation are in Binance's
[official Spot WebSocket documentation](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#how-to-manage-a-local-order-book-correctly).
The production stream schema separately identifies `U` and `u` in the
[official Spot API reference](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams#diff-depth-stream).

### Binance Spot implementation rule

- Maintain a Spot-specific synchronizer. Do not share a first-event policy
  with Futures merely because both payloads contain `U` and `u`.
- Before the snapshot bridge succeeds, buffer deltas but expose no book.
- After synchronization, treat `u <= local_update_id` as already covered.
  Treat `U > local_update_id + 1` as a hard gap. Apply only events that cover
  the next required ID.
- On a hard gap, clear all levels and update IDs before obtaining another
  snapshot. A stale pre-gap book must never remain queryable.
- Preserve the REST receive time separately. A REST snapshot has no
  WebSocket receive timestamp and cannot be used as network-latency evidence.

### Binance Spot documented ambiguity

The current official Spot guide contains two slightly different statements:
its bootstrap step says the first retained event should contain
`lastUpdateId` in `[U, u]`, while its general update rule declares a gap only
when `U > local_update_id + 1`. The latter admits the boundary case
`U == lastUpdateId + 1`; the former wording does not.

The collector should implement and test the explicit no-gap condition
`U <= lastUpdateId + 1 <= u`, record the first bridge IDs in diagnostics, and
keep a fixture for `U == lastUpdateId + 1`. This choice follows the documented
gap rule, but the wording discrepancy must remain visible until Binance
clarifies it. It must not be presented as an exchange guarantee.

The snapshot is depth-limited. Levels outside the returned 5,000 per side are
unknown until changed; the local book is not an L3 book and does not prove FIFO
queue position.

## 2. Binance USD-M Futures diff depth

### Binance Futures official facts

USD-M Futures uses a different bridge rule:

1. Buffer WebSocket events and request the Futures REST depth snapshot.
2. Drop events whose `u < snapshot.lastUpdateId`.
3. The first processed event must satisfy
   `U <= snapshot.lastUpdateId <= u`.
4. For each subsequent processed event, `pu` must equal the previous
   processed event's `u`; otherwise restart synchronization.
5. Quantities are absolute and zero removes a level.

Binance states these rules directly in the
[official USD-M local-order-book guide](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly).
The same guide fixes the REST snapshot limit used by this procedure at 1,000.

### Binance Futures implementation rule

- Select the Futures policy from the configured source/endpoint, not by the
  optional presence of `pu`.
- Validate the first retained event with `U <= S <= u`, where `S` is snapshot
  `lastUpdateId`.
- Do **not** require the first retained event's `pu` to equal `S`. The official
  bootstrap rule uses `U`/`u`; `pu` continuity becomes meaningful between the
  first processed event and each event after it.
- After the bridge, require `event.pu == previous_processed_u` for every event.
  Any mismatch is a hard gap and requires a fresh snapshot.
- Keep Spot and Futures regression fixtures separate. A Spot fixture that
  omits `pu` does not validate Futures behavior.

### Binance Futures unknown or not guaranteed

The public guide does not promise that a REST request will immediately return
a snapshot new enough to bridge the current buffer. The collector therefore
needs bounded retry/backoff and must remain unavailable while retrying.
Snapshot depth is limited to 1,000 levels, so deeper untouched levels remain
unknown.

## 3. OKX `books`

### OKX official facts

For the incremental `books` channel, OKX sends a 400-level initial full
snapshot and then changes every 100 ms. A snapshot has `prevSeqId == -1`.
Normally an update's `prevSeqId` equals the prior message's `seqId`.

OKX documents two exceptions:

- A no-change keepalive has empty `asks` and `bids`, with
  `seqId == prevSeqId == the last sequence`.
- During maintenance, sequence IDs may reset. The reset is itself an
  incremental message whose `seqId` is smaller than `prevSeqId`; the following
  update continues from the new smaller `seqId`. The official example is
  `snapshot 10 -> update 15 -> heartbeat 15 -> reset 3 -> update 5`.

The full snapshot, sequence rules, exceptions, and example are in the
[official OKX order-book channel documentation](https://www.okx.com/docs-v5/en/#websocket-api-public-channel-order-book-channel).
OKX also deprecated checksum validation for `books` on 2026-06-23 and directs
clients to use `seqId`/`prevSeqId` in its
[official API changelog](https://www.okx.com/docs-v5/log_en/#2026-06-23).

### OKX implementation rule

- Reject a purported `snapshot` unless `prevSeqId == -1`.
- For a normal update, require `prevSeqId == local_seq_id` before mutation.
- Accept the documented empty heartbeat without changing levels or the local
  sequence.
- If `prevSeqId == local_seq_id` and `seqId < prevSeqId`, apply the maintenance
  reset message transactionally and set the local sequence to the smaller
  `seqId`. The next update must link to that new value.
- Any other continuity failure invalidates the entire local book.
- Because `books` supplies its full image on initial subscription, an
  invalidated collector should force a reconnect or unsubscribe/resubscribe
  and wait for a new `snapshot`. It must ignore updates while unsynchronized.
- Do not validate the deprecated checksum or interpret its current zero value
  as a valid integrity proof.

### OKX unknown or not guaranteed

OKX documents the initial subscription snapshot but does not document durable
replay, retransmission of missed updates, or an automatic mid-connection
snapshot after a detected client-side gap. Consequently, continuing to consume
updates while waiting passively for a snapshot is not a safe recovery strategy.

The documentation does not define an atomicity guarantee for multiple objects
inside one `data` array. Parse and validate the complete received array before
committing state, and preserve its original receive order.

## 4. Polymarket CLOB `book` and `price_change`

### Polymarket official facts

The public market channel is L2 aggregated market data:

- `book` is a full aggregated order-book snapshot. It is emitted when first
  subscribed and when a trade affects the book.
- `price_change` is emitted for a new order or cancellation. One message may
  contain changes for multiple asset IDs.
- Each change carries an absolute size; size `"0"` removes the price level.
- `book` messages show a top-level `hash`; current `price_change` messages show
  a hash on each change.
- Both message types include a millisecond-looking `timestamp` field.

The message shapes and triggers are documented in the
[official Polymarket market-channel guide](https://docs.polymarket.com/market-data/websocket/market-channel)
and the
[official WebSocket API reference](https://docs.polymarket.com/api-reference/wss/market).
Polymarket's changelog documents an optional `initial_dump` subscription field
whose default is `true` in the
[2025-05-28 WebSocket change](https://docs.polymarket.com/changelog#may-28-2025).

### Polymarket implementation rule

- On every new connection, reconnect, or token resubscription, invalidate that
  token's prior book immediately and request/retain `initial_dump=true`.
- Keep the current and look-ahead market pairs on separate physical CLOB
  connections. A connection-level fault invalidates only the Up/Down pair on
  that connection. Under ingest v12 each pair connects only from
  `t0 - polymarket_capture_lead_seconds` through
  `t0 + opening_handoff_delay_seconds`; the baseline is `[-90s, +200s)`.
  The extra 20 seconds cover an order placed at the final `t0+180s` decision,
  its 15-second maximum work period and the configured P99 cancel race.
  Binance and Chainlink subscriptions remain continuous outside this interval.
  Planned CLOB inactivity is excluded from required-feed health rather than
  reported as a disconnect.
- In ingest v13, market rotation must not reconstruct the shared collector.
  Newly discovered CLOB windows are registered dynamically, completed windows
  are retired, and the durable session inventory rotates under the ingress lock
  only after the current `t0+200s` CLOB handoff and a complete flush. The
  Binance/Chainlink socket tasks and their quality validators remain alive, so
  rotation does not create the former transport-level one-second data gap.
- In ingest v15, continuous high-frequency subscriptions must detect business
  payload inactivity independently of WebSocket keepalive. Polymarket's
  [official RTDS documentation](https://docs.polymarket.com/market-data/websocket/rtds)
  describes crypto updates as sub-second and requires application `PING` every
  five seconds; Binance's
  [official Spot stream documentation](https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md#klinecandlestick-streams-for-utc)
  documents `kline_1s` updates every second. The collector uses
  conservative 15-second and 10-second accepted-payload deadlines respectively.
  Control frames and rejected payloads do not extend those deadlines. Expiry
  enters the normal explicit-gap/epoch and exponential-reconnect path. Market
  CLOB, OKX, and trade-only feeds have no generic deadline because normal quiet
  periods are not bounded by their public protocols.
- A bounded CLOB interval must begin with a new physical subscription and the
  official `initial_dump=true` snapshot. Do not implement storage reduction by
  dropping pre-window deltas from an already-running socket: offline replay
  would then lack an authoritative starting snapshot.
- Do not apply `price_change` for a token until a fresh full `book` for that
  token has been accepted. A `book` received later is authoritative and
  replaces all levels.
- A structurally valid zero- or one-sided snapshot/delta is authoritative:
  persist it, clear the absent levels, expose no two-sided BBO, and do not
  reconnect the shared multi-token socket. Resolved BTC tokens have been
  observed producing one-sided terminal snapshots during the current/next
  market handoff; treating those snapshots as connection corruption creates
  false gaps in the still-active token.
- A crossed snapshot/delta remains invalid and must create explicit
  unavailable/gap evidence at its local receive time. It must not be silently
  dropped while offline research continues using the old book.
- Validate all changes in one received `price_change` message before committing
  any configured token state. This is a project fail-closed inference from the
  official multi-asset message shape, not a documented venue transaction
  guarantee.
- Treat the last matching change's venue-reported `best_bid` and `best_ask` as
  authoritative BBO bounds for that payload and remove locally retained levels
  that contradict them. Live BTC evidence shows that a trade can advance the
  reported BBO before the following full `book` arrives; rejecting that
  transient local cross would create a false connection gap. The raw
  `price_change` and subsequent full `book` remain the replay evidence.
- Store the venue `timestamp` as source metadata and store a separate local
  monotonic/wall-clock receive timestamp. Use local receipt as the causal
  availability boundary. Persist a collector-session admission sequence as the
  final tie-breaker because the venue does not guarantee timestamp uniqueness.
- A real-time research consumer receives a bounded non-blocking copy only after
  the durable collector assigns that admission sequence. Duplicate/rejected
  payloads are not published. Consumer overflow is sticky and must stop that
  research projection, but it must never block, drop, or terminate raw capture.
- If the socket disconnects, local backpressure drops a message, payload
  validation fails, or ordering becomes suspect, open a new book epoch and
  require a new WebSocket snapshot. A REST `/book` response can be used for
  audit, but without a documented shared sequence it cannot prove a
  gap-free REST-snapshot-to-WebSocket bridge. The REST endpoint is documented
  as a snapshot in the
  [official order-book API](https://docs.polymarket.com/trading/orderbook).

### Polymarket unknown or not guaranteed

The current public documentation does **not** define:

- which server clock or matching-engine stage produces market-channel
  `timestamp`;
- monotonicity or uniqueness of that timestamp;
- a top-level sequence number, previous hash, or deterministic delta-chain
  rule;
- durable replay, a reconnect cursor, or delivery of messages missed while
  disconnected;
- a maximum delay from subscribe/resubscribe to the initial `book`;
- a guarantee that source-timestamp order equals socket delivery order.

Therefore `timestamp` alone cannot prove transport latency or message
continuity. Source-time regression thresholds are conservative project policy,
not Polymarket protocol facts. A subscription-snapshot timeout must leave the
book unavailable and retry with bounded backoff; it must not promote the last
pre-disconnect book.

## Acceptance fixtures derived from the official boundaries

The collector should retain the following protocol fixtures:

1. Spot: stale update, exact next ID, overlapping first event, missing-ID gap,
   and the documented `U == S + 1` ambiguity.
2. Futures: a valid first overlap whose `pu != S`, followed by valid `pu`
   chaining and a `pu` mismatch.
3. OKX: snapshot `prevSeqId=-1`, normal update, empty heartbeat, official
   `10 -> 15 -> 15 -> 3 -> 5` reset sequence, and forced resubscription after a
   real mismatch.
4. Polymarket: snapshot-before-delta, reconnect invalidation, multi-token
   validation failure with no half-commit, and invalid L2 evidence that makes
   the offline feature state unavailable at the same receive timestamp.

These are protocol-correctness tests. They are separate from strategy
performance, queue heuristics, and PnL tests.

## Chainlink 60-second TWAP boundary

The RTDS 60-second TWAP stream is a separate raw source. Its inner timestamp is
the observation time, its outer timestamp is publication time, and local receipt
is the availability boundary. The exact signed E18 field is retained and divided
by `10^18` with `Decimal`; the display value is only a tolerance-checked
diagnostic. RTDS has no reconnect history or snapshot, so a missing report is a
gap and never a locally synthesized settlement value. Point and TWAP streams
must each emit their own reconnect gap and recovery event.
