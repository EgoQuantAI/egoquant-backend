# Peak memory of goal requests by horizon (512 MB server)

Offline, real HTTP layer, regime sampler on, peak working set per case in a fresh process (Windows). Budget used here: 450 MB (512 MB minus headroom). `eval/memory/horizon_memory.py`.

| Mode | Investor | Years | Status | Time | Idle | Peak | Within budget |
|---|---|---|---|---|---|---|---|
| single | balanced_saver | 10 | 200 | 29.47 s | 168.7 MB | 244.5 MB | yes |
| single | balanced_saver | 25 | 200 | 33.71 s | 169.0 MB | 276.9 MB | yes |
| single | balanced_saver | 30 | 200 | 30.05 s | 168.8 MB | 286.5 MB | yes |
| single | balanced_saver | 40 | 200 | 29.34 s | 168.3 MB | 307.2 MB | yes |
| single | balanced_saver | 60 | 200 | 30.26 s | 168.1 MB | 347.6 MB | yes |
| single | growth_investor | 10 | 200 | 10.75 s | 168.1 MB | 267.9 MB | yes |
| single | growth_investor | 25 | 200 | 11.28 s | 168.3 MB | 297.7 MB | yes |
| single | growth_investor | 30 | 200 | 11.07 s | 168.2 MB | 307.9 MB | yes |
| single | growth_investor | 40 | 200 | 11.47 s | 168.4 MB | 328.5 MB | yes |
| single | growth_investor | 60 | 200 | 12.45 s | 168.3 MB | 368.3 MB | yes |
| single | crypto_trader | 10 | 200 | 9.59 s | 168.4 MB | 299.1 MB | yes |
| single | crypto_trader | 25 | 200 | 9.89 s | 168.4 MB | 327.6 MB | yes |
| single | crypto_trader | 30 | 200 | 9.71 s | 168.2 MB | 338.1 MB | yes |
| single | crypto_trader | 40 | 200 | 9.94 s | 168.2 MB | 356.5 MB | yes |
| single | crypto_trader | 60 | 200 | 10.47 s | 168.7 MB | 397.5 MB | yes |
| whatif | balanced_saver | 10 | ok | 1.02 s | 168.4 MB | 248.4 MB | yes |
| whatif | balanced_saver | 25 | ok | 1.03 s | 168.5 MB | 256.8 MB | yes |
| whatif | balanced_saver | 30 | ok | 1.04 s | 168.4 MB | 263.1 MB | yes |
| pair | balanced_saver | 25 | [200, 200] | 78.65 s | 168.7 MB | 351.3 MB | yes |
| pair | balanced_saver | 30 | [200, 200] | 81.92 s | 168.5 MB | 369.3 MB | yes |
| pair | balanced_saver | 60 | [200, 200] | 90.76 s | 168.7 MB | 499.1 MB | **no** |
