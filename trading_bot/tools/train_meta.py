"""
메타 라벨링 모델 학습 파이프라인.

  1) DB signals 테이블에서 아직 라벨이 없는 1차 BUY 시그널을 읽는다.
  2) 같은 종목의 bars 테이블로 삼중 장벽 라벨(y ∈ {0,1})을 붙인다.
  3) Purged K-Fold (embargo 포함) 로 LightGBM 을 교차검증한다.
  4) 전체 데이터로 재학습해 models/meta_lgbm.pkl 로 저장한다.

일반 K-Fold 는 금융 시계열의 자기상관 때문에 미래 정보가 새어 들어간다.
López de Prado 의 Purging(테스트 구간과 보유기간이 겹치는 학습 샘플 제거) 과
Embargo(테스트 직후 일정 비율 추가 배제) 를 적용해 그 누수를 막는다.

실행: python -m trading_bot.tools.train_meta [--min-samples 300] [--dry]
"""
from __future__ import annotations

import argparse
import logging
import pickle
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..config import settings as cfg
from ..core.meta_filter import FEATURE_ORDER, label_triple_barrier
from ..database.db import Database

log = logging.getLogger("train_meta")


# ------------------------------------------------------------------ 라벨링
def label_pending(db: Database) -> int:
    """미라벨 시그널에 삼중 장벽 라벨을 붙인다. 붙인 개수를 반환."""
    pending = db.unlabeled_signals("BUY")
    if not pending:
        log.info("라벨링할 시그널이 없습니다")
        return 0

    labeled = 0
    for sig in pending:
        bars = db.bars_after(sig["code"], sig["ts"])
        if len(bars) < 5:
            continue  # 아직 관측 구간이 부족 — 다음 실행에서 다시 시도
        idx = pd.to_datetime([b[0] for b in bars])
        prices = pd.Series([b[1] for b in bars], index=idx)

        entry_time = pd.Timestamp(sig["ts"])
        deadline = entry_time + timedelta(minutes=cfg.TB_VERTICAL_MIN)
        if prices.index[-1] < deadline and datetime.now() - entry_time < timedelta(days=1):
            continue  # 수직 장벽까지 데이터가 아직 안 찼다

        entry_price = float(sig["price"] or 0.0)
        atr = float(sig["features"].get("atr_pct", 0.0)) * entry_price
        if atr <= 0:
            atr = entry_price * 0.005  # ATR 이 없으면 0.5% 를 대용

        y, reason, ret = label_triple_barrier(prices, entry_time, entry_price, atr)
        db.set_label(sig["id"], y, reason, ret)
        labeled += 1

    log.info("라벨링 완료: %d건", labeled)
    return labeled


# ------------------------------------------------------------------ 교차검증
def purged_kfold_indices(n: int, n_splits: int = 5, embargo_pct: float = 0.02):
    """
    시간순 정렬된 샘플에 대한 Purged K-Fold.
    테스트 폴드 앞뒤로 embargo 구간을 학습셋에서 제외한다.
    """
    indices = np.arange(n)
    fold_size = n // n_splits
    embargo = int(n * embargo_pct)

    for k in range(n_splits):
        start = k * fold_size
        stop = n if k == n_splits - 1 else (k + 1) * fold_size
        test_idx = indices[start:stop]
        left = indices[: max(start - embargo, 0)]
        right = indices[min(stop + embargo, n):]
        train_idx = np.concatenate([left, right])
        if len(train_idx) == 0 or len(test_idx) == 0:
            continue
        yield train_idx, test_idx


def deflated_sharpe(sharpe: float, n_trials: int, n_obs: int,
                    skew: float = 0.0, kurt: float = 3.0) -> float:
    """
    Deflated Sharpe Ratio 의 p-value.
    여러 하이퍼파라미터를 시도하면 우연히 높은 샤프가 나오는데(다중 검정 편향),
    그 기대 최대치를 넘어서는지 검정한다.
    """
    from scipy.stats import norm

    if n_obs < 2 or n_trials < 1:
        return float("nan")
    euler = 0.5772156649
    e_max = (1 - euler) * norm.ppf(1 - 1 / n_trials) + euler * norm.ppf(1 - 1 / (n_trials * np.e))
    denom = np.sqrt(1 - skew * sharpe + (kurt - 1) / 4 * sharpe**2)
    if denom <= 0:
        return float("nan")
    z = (sharpe - e_max) * np.sqrt(n_obs - 1) / denom
    return float(norm.cdf(z))


# ------------------------------------------------------------------ 학습
def train(db: Database, min_samples: int = 300, dry: bool = False) -> dict | None:
    try:
        import lightgbm as lgb
    except ImportError:
        log.error("lightgbm 이 설치되어 있지 않습니다: pip install lightgbm")
        return None
    from sklearn.metrics import roc_auc_score

    rows = db.labeled_signals()
    if len(rows) < min_samples:
        log.error("학습 샘플 부족: %d < %d — 모의투자를 더 돌려 데이터를 모으세요",
                  len(rows), min_samples)
        return None

    rows.sort(key=lambda r: r["ts"])
    X = pd.DataFrame([[float(r["features"].get(f, np.nan)) for f in FEATURE_ORDER] for r in rows],
                     columns=FEATURE_ORDER)
    y = np.array([int(r["label"]) for r in rows])

    pos_rate = y.mean()
    log.info("샘플 %d건, 성공(y=1) 비율 %.1f%%", len(y), pos_rate * 100)
    if pos_rate < 0.05 or pos_rate > 0.95:
        log.warning("라벨이 한쪽으로 심하게 치우쳐 있습니다 — 장벽 배수를 조정하세요")

    params = dict(
        objective="binary",
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=30,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        class_weight="balanced",
        random_state=42,
        verbose=-1,
    )

    aucs, returns = [], []
    for fold, (tr, te) in enumerate(purged_kfold_indices(len(y), n_splits=5, embargo_pct=0.02), 1):
        if len(np.unique(y[tr])) < 2 or len(np.unique(y[te])) < 2:
            log.warning("fold %d: 한쪽 클래스만 존재 — 건너뜀", fold)
            continue
        model = lgb.LGBMClassifier(**params).fit(X.iloc[tr], y[tr])
        proba = model.predict_proba(X.iloc[te])[:, 1]
        auc = roc_auc_score(y[te], proba)
        aucs.append(auc)
        # 임계치 통과 샘플만 매매했을 때의 성공률
        taken = proba >= cfg.META_PROB_THRESHOLD
        hit = y[te][taken].mean() if taken.sum() else float("nan")
        returns.append(hit)
        log.info("fold %d: AUC=%.3f  진입 %d/%d  성공률 %.1f%% (필터 전 %.1f%%)",
                 fold, auc, taken.sum(), len(te),
                 0.0 if np.isnan(hit) else hit * 100, y[te].mean() * 100)

    if not aucs:
        log.error("유효한 폴드가 없습니다")
        return None

    mean_auc = float(np.mean(aucs))
    log.info("교차검증 평균 AUC = %.3f (±%.3f)", mean_auc, float(np.std(aucs)))
    if mean_auc < 0.55:
        log.warning("AUC 가 낮습니다(%.3f). 이 모델을 켜면 오히려 손해일 수 있습니다.", mean_auc)

    model = lgb.LGBMClassifier(**params).fit(X, y)
    importance = sorted(zip(FEATURE_ORDER, model.feature_importances_), key=lambda t: -t[1])
    log.info("피처 중요도: %s", ", ".join(f"{k}={v}" for k, v in importance))

    result = {
        "model": model,
        "features": FEATURE_ORDER,
        "cv_auc": mean_auc,
        "n_samples": len(y),
        "pos_rate": float(pos_rate),
        "trained_at": datetime.now().isoformat(timespec="seconds"),
        "params": params,
    }

    if dry:
        log.info("--dry 지정 — 모델을 저장하지 않았습니다")
        return result

    with open(cfg.META_MODEL_PATH, "wb") as fh:
        pickle.dump(result, fh)
    log.info("모델 저장: %s", cfg.META_MODEL_PATH)
    log.info("이제 .env 에서 META_FILTER_ENABLED=true 로 바꾸면 필터가 적용됩니다.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="메타 라벨링 모델 학습")
    parser.add_argument("--min-samples", type=int, default=300, help="최소 학습 샘플 수")
    parser.add_argument("--label-only", action="store_true", help="라벨링만 하고 종료")
    parser.add_argument("--dry", action="store_true", help="학습하되 저장하지 않음")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    db = Database()
    label_pending(db)
    if args.label_only:
        return
    if train(db, args.min_samples, args.dry) is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
