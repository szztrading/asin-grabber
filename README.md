# Competitor ASIN Grabber (Streamlit)

输入一个竞品 ASIN（如 `B0D4QMBS75`），抓取该商品详情页的相似/相关推荐位 ASIN，尽量补全标题、价格、评分、评论，并支持导出 CSV。  
支持 Keepa API（优先使用 alsoBought/alsoViewed），如未配置则回退 HTML 解析。

## 本地运行
```bash
pip install -r requirements.txt
streamlit run app.py
