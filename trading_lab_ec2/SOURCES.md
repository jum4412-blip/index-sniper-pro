# Primary technical references

Checked 2026-10-13. These describe third-party interfaces; they do not certify this package. Links are provided as literal technical references.

## Bitget public V3 market API

```text
https://www.bitget.com/docs/catalog/market/market-data
GET https://api.bitget.com/api/v3/market/tickers?category=USDT-FUTURES&symbol=BTCUSDT
GET https://api.bitget.com/api/v3/market/candles?category=USDT-FUTURES&symbol=BTCUSDT&interval=5m
```

The collector uses only the public ticker/candle endpoints, with the same requests for ETHUSDT. It does not sign requests or access an account. Snapshot fundingRate is not a realized user payment. 1W is not used as a supported interval in this build.

## GitHub browser uploads and sensitive data

```text
https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository
```

GitHub browser folder upload and branch/PR handling; credentials must not be committed. Browser uploads do not automatically honor line-ending transformations in .gitattributes.

## Git commit snapshot extraction

```text
https://git-scm.com/docs/git-archive
```

Deploy reads one committed folder into a separate staging directory instead of updating the live worktree.

## systemd process service and timer configuration

```text
https://github.com/systemd/systemd/blob/main/man/systemd.service.xml
https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml
```

The units configure on-failure restart, a software watchdog, and a persistent daily timer. Start-rate limits still apply; these are not guarantees of uninterrupted EC2 operation.

## Telegram sendMessage

```text
https://core.telegram.org/bots/api#sendmessage
```

Only used when the user provides local Telegram credentials. No credential was supplied or tested in the build environment.

## AWS EBS snapshots

```text
https://docs.aws.amazon.com/ebs/latest/userguide/ebs-snapshots.html
```

EBS snapshots are separate point-in-time backups. This package's local SQLite snapshots are not EBS snapshots, and it does not provision AWS backup infrastructure.
