# Muse 对照数据

把 Muse 4h 盘的已平仓记录放到 `muse/trades_4h.csv`，列与 `accounts/4h/trades.csv` 相同：

```
coin,side,entry_time,entry,exit_time,exit,reason,net_pnl
```

时间用新加坡时间 `YYYY-MM-DD HH:MM`。周报（`report.py`）会自动读入并并列对比。
