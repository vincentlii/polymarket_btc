# BTC Chainlink 60-second TWAP evidence

## Current boundary

From `2026-08-07T00:00:00Z`, the active BTC 15m rule epoch is
`chainlink-btc-usd-twap-60s-v1`. Gamma rule-bearing metadata, rather than the
market date, classifies the contract. The caller's expected epoch is verified
against that classification and is never used to relabel a market.

The collector separately subscribes to RTDS `crypto_prices_twap_sixty` updates
with `type: update` and compact `{"symbol":"btc/usd"}` filtering. It keeps
`payload.timestamp` as Chainlink observation time, the outer `timestamp` as
publication time, and collector receipt as local availability evidence.
`full_accuracy_value` remains the exact signed E18 string; numeric `value` is
only a display value. Runtime derives the exact `Decimal` from the signed E18
integer divided by `10^18`; a supplied display value is diagnostic-only and
must agree within the documented display tolerance. The point-price RTDS stream
remains separate.

## Fail-closed limits

RTDS has no snapshot, history, or replay after reconnect. Missing or stale
TWAP evidence therefore creates a distinct quality gap and is never locally
reconstructed. The 60-second window is not a publication cadence; sampling,
weighting, rounding, and missing-input behaviour are not inferred locally.

No TWAP-specific probability model has been trained, validated, or promoted.
The current TWAP evidence is insufficient for promotion. Cross-epoch artifact
loads reject by default. The only exception is the explicitly configured,
status-labelled Research Paper transition proxy: a point-epoch artifact may
observe a TWAP-epoch market only when
`allow_rule_epoch_transition_proxy=true`. That exception is non-promotable and
unavailable to Shadow, Canary, or live paths. Old artifacts without
`metadata.config.rule_epoch` fail closed; operators must not silently infer or
backfill that metadata.

Source: [Polymarket Chainlink TWAP documentation](https://docs.polymarket.com/market-data/chainlink-twap).
