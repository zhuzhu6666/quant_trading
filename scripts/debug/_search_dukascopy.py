#!/usr/bin/env python
"""搜索 Dukascopy tick 数据 Python 库"""
import urllib.request, json

url = "https://api.github.com/search/repositories?q=dukascopy+tick+data+python&sort=stars&per_page=20"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
resp = urllib.request.urlopen(req, timeout=10)
data = json.loads(resp.read())

print(f"找到 {data['total_count']} 个仓库")
print("=" * 70)
for item in data['items'][:15]:
    print(f"⭐ {item['stargazers_count']:>4} | {item['full_name']}")
    desc = (item['description'] or "无描述")[:120]
    print(f"   {desc}")
    print(f"   {item['html_url']}")
    print(f"   最后更新: {item['updated_at'][:10]}")
    print()
