# -*- coding: utf-8 -*-
import sys
try: sys.stdout.reconfigure(encoding='utf-8')
except Exception: pass
import FinanceDataReader as fdr
for code in ['KS11','KQ11']:
    df = fdr.DataReader(code, '2026-08-17')
    print(code)
    print(df[['Close']].tail(8).to_string())
    c = df['Close']
    if len(c) >= 2:
        print("  마지막 봉 등락률 %.2f%%" % ((c.iloc[-1]/c.iloc[-2]-1)*100))
    print()
