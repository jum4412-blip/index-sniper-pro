# Test and verification report

Build date: 2026-10-13. Package: 0.2.0-observe.

## Executed in the build container

- Python 3.13.5, Git 2.47.3.
- Original risk-sizing/NAV code: 41 tests passed.
- Observer/deployment code: 61 tests passed.
- Total: **102 tests passed**. All tests use synthetic market data and temporary local databases.
- Bash parsing of all package shell scripts: passed.
- Six local launcher smoke checks (offline doctor, not-started status, fictional demo, uninitialized ledger rejection, no-database backup, paths): expected results. No fake account/NAV database was created. See CLI_SMOKE_TEST.json.
- `systemd-analyze verify` on rendered service/timer files: exit status 0.
- Git deployment isolation integration fixture: verified the requested commit's folder can be extracted while the existing live branch, HEAD and dirty tracked file stay unchanged. The fixture uses a stub installer: it does not simulate or execute real sudo/systemd deployment.
- CSV/XLSX/PDF output is not part of this package.

## Important checks covered

No authenticated exchange request or order route is implemented. An attempt to use an account/order path in the public client is rejected before a network call. Only OBSERVE_ONLY config is accepted. NaN/nonpositive prices, wrong symbols/categories, stale ticker data, clock skew, unclosed candles, invalid OHLC and negative volume are rejected. Duplicates are idempotent, candle corrections are audited, missing stored candles remain visible, and timestamps cannot silently shift phase between cycles. Collector process locking, atomic status file writing, restart-persistent SQLite storage, same-instance backup integrity and seven-snapshot rotation were exercised. Telegram errors do not echo the token. Original NAV tests include deposit-neutral returns.

## Not validated here

- Actual public Bitget endpoint connectivity: attempts failed with DNS resolution errors in the build environment. Documentation-derived schemas were tested with fixtures, not current exchange responses.
- Actual EC2 host Python/systemd versions, RAM/disk/network capacity, security group/IAM configuration.
- Live systemd lifecycle, crash/reboot recovery on the user's host.
- Telegram transmission/receipt, because no credentials were supplied or accessed.
- Existing user's GitHub files, CCI/CHOP code, live positions, protection orders, account equity or fills.
- Strategy performance, future returns, real trading execution, automatic reconciliation, authenticated account APIs.

Run `~/lab1030 doctor`, then `~/lab1030 start`, and inspect `~/lab1030 status` on the target EC2. A local test pass is not an exchange compatibility certification or evidence of profitability.

The included `TEST_OUTPUT.txt` and `SYSTEMD_VERIFY.txt` are outputs from the actual local checks. The original core also retains its previous test documentation as historical context.
