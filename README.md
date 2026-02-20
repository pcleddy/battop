# mac_utils

Minimal, htop-style terminal monitors for macOS. No dependencies beyond Python 3.

## battop

Battery history chart with charging indicators.

![battop screenshot](screenshot.png)

```bash
python3 battop.py
```

Keys: `q` quit · `r` refresh · `c` clear history

History persists to `~/.battop_history.json` (last 24h). Reads from `pmset` and `ioreg`.

## netop

Network traffic chart — received (cyan, up) and sent (red, down) in a mirrored layout.

```bash
python3 netop.py
```

Keys: `q` quit · `r` refresh · `c` clear history · `i` cycle interface

Auto-detects active interfaces. History persists to `~/.netop_history.json` (last 1h). Reads from `netstat -ib`.
