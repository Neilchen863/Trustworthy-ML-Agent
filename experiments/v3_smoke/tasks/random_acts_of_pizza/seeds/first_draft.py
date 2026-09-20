import json

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Neutral baseline used as the pinned first draft of every replicate.  It only uses fields that also exist
# at prediction time (request text/title and the *_at_request numbers); every preprocessing step lives
# inside the Pipeline, so it is fit on the training part of each fold only.

with open("./input/train/train.json", "r") as f:
    train_df = pd.DataFrame(json.load(f))
with open("./input/test/test.json", "r") as f:
    test_df = pd.DataFrame(json.load(f))

NUMERIC = [
    "requester_account_age_in_days_at_request",
    "requester_days_since_first_post_on_raop_at_request",
    "requester_number_of_comments_at_request",
    "requester_number_of_comments_in_raop_at_request",
    "requester_number_of_posts_at_request",
    "requester_number_of_posts_on_raop_at_request",
    "requester_number_of_subreddits_at_request",
    "requester_upvotes_minus_downvotes_at_request",
    "requester_upvotes_plus_downvotes_at_request",
]


def make_frame(df):
    out = df[NUMERIC].fillna(0).astype(float).copy()
    out["text"] = (df["request_title"].fillna("") + " " + df["request_text_edit_aware"].fillna(""))
    return out


X, y = make_frame(train_df), train_df["requester_received_pizza"].astype(int).values
X_test = make_frame(test_df)


def build_model():
    features = ColumnTransformer(
        [
            ("text", TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_features=20000, sublinear_tf=True), "text"),
            ("num", StandardScaler(), NUMERIC),
        ]
    )
    return Pipeline([("features", features), ("clf", LogisticRegression(C=1.0, max_iter=2000))])


cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=0)
scores = []
for index, (tr, va) in enumerate(cv.split(X, y)):
    model = build_model().fit(X.iloc[tr], y[tr])
    score = roc_auc_score(y[va], model.predict_proba(X.iloc[va])[:, 1])
    scores.append(score)
    print(f"[fold] index={index} name=roc_auc value={score:.6f}")
print(f"[metric] name=roc_auc value={float(np.mean(scores)):.6f}")

final_model = build_model().fit(X, y)
submission = pd.DataFrame(
    {"request_id": test_df["request_id"], "requester_received_pizza": final_model.predict_proba(X_test)[:, 1]}
)
submission.to_csv("./submission/submission.csv", index=False)
print("submission rows:", len(submission))
