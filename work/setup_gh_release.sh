#!/usr/bin/env bash
# Tek seferlik kurulum: outputs/ GitHub Release'e yükler.
# Workflow'u devreye almadan önce bir kez çalıştır.
#
# Gereksinim: gh CLI (brew install gh) + gh auth login yapılmış olmalı.
#
# Kullanım:
#   bash work/setup_gh_release.sh

set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== outputs/ arşivleniyor ==="
ARCHIVE=/tmp/outputs.tar.gz
tar -czf "$ARCHIVE" outputs/
SIZE=$(du -sh "$ARCHIVE" | cut -f1)
echo "Arşiv: $ARCHIVE ($SIZE)"

echo ""
echo "=== outputs-data release oluşturuluyor (zaten varsa geçilir) ==="
gh release view outputs-data >/dev/null 2>&1 && echo "Release zaten var, atlanıyor." || \
  gh release create outputs-data \
    --title "Data outputs (otomatik güncellenir)" \
    --notes "Ham scrape verileri. GitHub Actions tarafından her gün güncellenir. Manuel düzenleme yapma." \
    --prerelease

echo ""
echo "=== outputs.tar.gz yükleniyor ($SIZE) ==="
gh release upload outputs-data "$ARCHIVE" --clobber

echo ""
echo "=== Bitti ==="
echo "Şimdi .github/workflows/daily-refresh.yml commit + push edebilirsin."
echo "  git add .github/workflows/daily-refresh.yml && git commit -m 'ci: daily refresh workflow' && git push"
