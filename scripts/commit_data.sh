#!/usr/bin/env bash
# data/ 에 변경이 있으면 커밋·푸시한다. meta.json 만 바뀐 경우(호출 횟수 등)는 커밋하지 않음.
# 사용: bash scripts/commit_data.sh "공고"
set -u
label="${1:-수집}"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A data
if git diff --cached --name-only | grep -v '^data/meta.json$' | grep -q .; then
  git commit -m "data: ${label} $(TZ=Asia/Seoul date +'%Y-%m-%d %H:%M')"
  for i in 1 2 3 4; do
    git pull --rebase -X theirs origin "${GITHUB_REF_NAME}" && git push && break
    sleep $((i * 5))
  done
else
  echo "변경 없음"
fi
