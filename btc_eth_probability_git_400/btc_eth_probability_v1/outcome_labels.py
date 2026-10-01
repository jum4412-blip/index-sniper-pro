"""No network or orders. Independent hypothetical events, never a portfolio result."""
from __future__ import annotations
import ctypes as ct
import os
from pathlib import Path
import subprocess
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parent
FIELDS=['entry_ms','label_end_ms','entry_mid','entry_fill','qty','initial_stop','target','risk','exit_reference','exit_fill','gross','fees','funding','net','net_r','label_profitable','hold_minutes','gross_r','reason_code','execution_eligible','actual_stop_fraction','notional']
REASONS={1:'STOP',2:'TARGET',3:'MAX_HOLD',11:'OUTSIDE_DATA',12:'STOP_INVALID',13:'ENTRY_GAP',14:'ADVERSE_LAST_FUNDING',15:'RISK_EXCEEDS_25',16:'STRUCTURAL_STOP_OUTSIDE_FROZEN_RANGE'}
_LIB=None

def compile_kernel():
    temporary=ROOT/'outcome_labels_native.build.so'
    subprocess.run(['g++','-O3','-std=c++17','-shared','-fPIC',str(ROOT/'outcome_labels.cpp'),'-o',str(temporary),'-Wall','-Wextra'],check=True)
    os.replace(temporary,ROOT/'outcome_labels_native.so')

def label_events(minute,funding,events,symbol,*,cost=1.,path=2):
    global _LIB
    if not (ROOT/'outcome_labels_native.so').exists():compile_kernel()
    if _LIB is None:
        _LIB=ct.CDLL(str(ROOT/'outcome_labels_native.so'))
        dp=ct.POINTER(ct.c_double)
        _LIB.label_events.argtypes=[dp,ct.c_int64,dp,ct.c_int64,dp,ct.c_int64,ct.c_int,ct.c_double,ct.c_int,dp]
        _LIB.label_events.restype=ct.c_int
    matrix=np.ascontiguousarray(events[['entry_after_ms','side','entry_reference','stop','max_hold_minutes']].to_numpy(dtype=np.float64))
    raw=np.ascontiguousarray(minute,dtype=np.float64) if not minute.flags['C_CONTIGUOUS'] else minute
    ff=np.ascontiguousarray(funding,dtype=np.float64)
    out=np.empty((len(matrix),len(FIELDS)),dtype=np.float64)
    dp=ct.POINTER(ct.c_double)
    err=_LIB.label_events(raw.ctypes.data_as(dp),len(raw),ff.ctypes.data_as(dp),len(ff),matrix.ctypes.data_as(dp),len(matrix),0 if symbol=='BTCUSDT' else 1,float(cost),int(path),out.ctypes.data_as(dp))
    if err:raise RuntimeError('native label replay failed '+str(err))
    if not np.isfinite(out).all():raise RuntimeError('nonfinite labels')
    result=pd.concat([events.reset_index(drop=True),pd.DataFrame(out,columns=FIELDS)],axis=1)
    if 'structure_valid' in result:
        invalid=~result.structure_valid.astype(bool)
        result.loc[invalid,'execution_eligible']=0
        result.loc[invalid,'reason_code']=16
    result['reason']=result.reason_code.map(REASONS)
    valid=result.execution_eligible==1
    if len(result[valid]) and not np.allclose(result.loc[valid,'gross']-result.loc[valid,'fees']+result.loc[valid,'funding'],result.loc[valid,'net'],rtol=1e-12,atol=1e-8):raise RuntimeError('label ledger mismatch')
    if (result.loc[valid,'risk']>25.00000001).any() or (result.loc[valid,'notional']>2500.00000001).any():raise RuntimeError('label sizing/risk breached')
    return result
