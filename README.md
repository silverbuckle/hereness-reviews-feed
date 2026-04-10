# HERENESS Reviews Feed

HERENESSのShopify商品にYotpoレビューデータを紐付けたJSONフィードを生成し、
GitHub Pagesで配信するリポジトリ。Klaviyoの Custom Catalog Source が
このJSONを6時間ごとに pull して、メール内でレビューを動的表示する。

## アーキテクチャ

```
GitHub Actions (daily 06:00 JST)
  ↓ Python
  ├── Shopify Admin API: 全商品取得
  ├── hereness.jp/products/* : Yotpo product ID スクレイピング
  ├── Yotpo Widget API: レビュー取得
  └── JSON生成 → docs/reviews.json
  ↓ git push
GitHub Pages
  https://silverbuckle.github.io/hereness-reviews-feed/reviews.json
  ↓ (6時間ごと)
Klaviyo Custom Catalog (integration_type: $custom)
  ↓
Klaviyo Email Template
  {% catalog %} タグで cart line items × カタログを結合してレビュー表示
```

## 出力フィード

`docs/reviews.json`:

```json
{
  "generated_at": "2026-04-10T21:00:00+00:00",
  "source": "yotpo+shopify",
  "store": "hereness.jp",
  "item_count": 70,
  "items": [
    {
      "id": "9375923929305",
      "handle": "hu-10014",
      "title": "DRY WOOL T-SHIRT 2(UNISEX)",
      "price": 13200,
      "url": "https://hereness.jp/products/hu-10014",
      "image_url": "...",
      "product_shot_url": "...",
      "product_type": "トップス",
      "tags": ["UNISEX", "ウール", ...],
      "average_score": 4.6,
      "total_reviews": 10,
      "top_review_text": "40代男性、172cm、66kgでMサイズを購入しました...",
      "top_review_score": 5,
      "top_review_title": "良い買い物をしました"
    }
  ]
}
```

## プライバシー設計

- **レビュアー名は含めない** — Yotpoが内部で匿名化していても完全除外
- `robots.txt` で検索エンジンにインデックスされないよう設定
- 個人識別可能な情報は一切含めない

## セキュリティ

- 公開URL（認証なし）だが、含まれるのはYotpoで既に公開されているレビュー文のみ
- Shopify Access Tokenは GitHub Actions Secrets で管理
- Yotpo App Keyは公開情報（storefrontのJSに露出している）

## セットアップ

### 1. GitHub Secrets

リポジトリの Settings → Secrets and variables → Actions で以下を設定:

- `SHOPIFY_ACCESS_TOKEN` — Shopify Admin API トークン（読み出し専用）
- `YOTPO_APP_KEY` — Yotpo Store ID

### 2. GitHub Pages

Settings → Pages で:
- Source: Deploy from a branch
- Branch: `main` / `/docs`

### 3. 初回実行

```bash
# Actions タブから "Daily Reviews Sync" を手動実行
gh workflow run daily.yml
```

## Klaviyoとの連携

### Custom Catalog Source の設定

Klaviyo管理画面 → Content → Products → Add New Source:

- Source Name: `Hereness Reviews`
- Type: Custom / URL Feed
- Feed URL: `https://silverbuckle.github.io/hereness-reviews-feed/reviews.json`
- External ID field: `id`
- Title field: `title`
- Image field: `image_url`
- Price field: `price`
- URL field: `url`
- Custom Metadata: すべての `average_score`, `total_reviews`, `top_review_text`, `top_review_score`, `top_review_title` をマップ

### メールテンプレートでの使用

Textブロック（HTMLモード）内で:

```django
{% for item in event.extra.line_items %}
  {% catalog item.product_id integration="api" catalog_id="HERENESS_REVIEWS_CATALOG_ID" %}
    <h3>{{ catalog_item.title }}</h3>
    {% if catalog_item.metadata|lookup:"average_score" %}
      <p>★ {{ catalog_item.metadata|lookup:"average_score" }}
         ({{ catalog_item.metadata|lookup:"total_reviews" }}件のレビュー)</p>
      <blockquote>
        "{{ catalog_item.metadata|lookup:"top_review_text" }}"
      </blockquote>
    {% endif %}
  {% endcatalog %}
{% endfor %}
```

## ローカル実行（テスト）

```bash
cd hereness-reviews-feed
export SHOPIFY_STORE_URL=hereness.myshopify.com
export SHOPIFY_ACCESS_TOKEN=shpat_xxxxx
export YOTPO_APP_KEY=0oKf86FVGfWRgAH35OnetKxc1VlFxnNxuEpNxct2

python3 scripts/sync_reviews.py
```

出力: `docs/reviews.json`

## 関連ドキュメント

- [Klaviyo Catalog Items API](https://developers.klaviyo.com/en/reference/catalogs_api_overview)
- [Klaviyo Custom Catalog Feed Guide](https://developers.klaviyo.com/en/docs/guide_to_syncing_a_custom_catalog_feed_to_klaviyo)
- [Catalog lookup tag reference](https://help.klaviyo.com/hc/en-us/articles/360004785571)
