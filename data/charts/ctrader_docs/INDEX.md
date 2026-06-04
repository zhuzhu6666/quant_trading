# cTrader Open API 文档抓取汇总

> 抓取时间: 2026-06-05
> 目的: (1) 验证"我能不能联网读文档" — 能。 (2) 为 `ctrader_bridge.py` App auth `wrong random id` 错误备料

## 文件清单

### A. GitHub raw markdown / SDK 源码 (9 个)

真源仓库: [`spotware/OpenApiPy`](https://github.com/spotware/OpenApiPy) 分支 `main`
(注意: Explore agent 最初给的 `master/docs/Open_API_Overview.md` 路径全 404 — 实际分支是 `main`, 文件名是 `authentication.md` / `client.md` / `index.md`, `.proto` 文件在 `OpenApiPy` 仓库不存在)

| 文件 | 体积 | 一句话内容 |
|---|---|---|
| `README.md` | 2.8KB | 仓库根,基本概念 + 安装命令 |
| `OpenApiPy_index.md` | 2.2KB | **Getting Started** — 完整 sample code,App auth 消息构造、Client.send、callback 写法 |
| `authentication.md` | 2.9KB | **OAuth2 流** — Auth 类、getAuthUri、getToken(auth_code)、refreshToken |
| `client.md` | 3.9KB | **Client 类** — 创建 Client、发送消息、cancel deferred、3 类 callback |
| `client.py` | 3.6KB | **SDK Client 实现真源** — `Client.send` 完整源码,看 `clientMsgId` 默认怎么用 |
| `auth.py` | 1.3KB | Auth 类实现 — OAuth code→token, refresh_token→token 流程 |
| `tcpProtocol.py` | 2.5KB | **wire format 真源** — `ProtoMessage` 容器怎么打包,`clientMsgId` 在哪一层上 wire |
| `protobuf.py` | 1.6KB | 消息类注册表 — `Protobuf.extract()` 把 wire bytes 解成具体 Res |

### B. help.ctrader.com SPA 抓取 (3 HTML + 1 PNG)

Angular SPA,Playwright 渲染后抽 `innerText`。SPA 实际子路径:
- ✅ `https://help.ctrader.com/open-api/` (Getting Started) — **根页**
- ✅ `https://help.ctrader.com/open-api/creating-new-app/` — OAuth 凭证创建流程
- ✅ `https://help.ctrader.com/open-api/error-handling/` — 错误处理
- ✅ `https://help.ctrader.com/open-api/messages/` — **完整 ProtoOA* 消息定义表**
- ❌ Explore agent 给的 `/open-api/protocol-messages/` `/getting-started/` `/application-authentication/` — **全 404,不存在**

| 文件 | 体积 | 来源 URL |
|---|---|---|
| `spa_creating-new-app.html` | 5.8KB | https://help.ctrader.com/open-api/creating-new-app/ |
| `spa_error-handling.html` | 4.7KB | https://help.ctrader.com/open-api/error-handling/ |
| `spa_messages.html` | 5.0KB | https://help.ctrader.com/open-api/messages/ |
| `screenshot_messages.png` | 64KB | 同上,viewport 截图 |

## 关键发现(为 `wrong random id` 修复备料)

### Finding 0 — 2026-06-05 实测复盘: token 不是问题,clientId/secret 才是

跑 `scripts/validate_ctrader_token.py`(新落)发现:
- **clientMsgId = UUID 也被拒**(`e4922bdf-ece2-4709-a00b-a793f43eca48` 过 wire,server 仍报 `CH_CLIENT_AUTH_FAILURE / wrong random id`)
- **clientSecret 故意写错也被拒,错误信息一字不差** → `wrong random id` 是 server 端**所有 App auth 失败**的统一描述,不暴露真因
- **server 收到错误响应后立即断连**(不给你发第二个请求)
- raw bytes 解 hex: `1a16 43...45` = field 3 = `CH_CLIENT_AUTH_FAILURE`,`22 0f 77...64` = field 4 = `wrong random id`(跟 proto 定义字段号一致)
- `Protobuf.extract()` 正常工作,SDK 字段名映射也对(`errorCode`, `description` 都能读到)

### Finding 0.1 — 2026-06-05 23:50 真凶: `.env` 里 `clientId/secret` 抄错了字符

用户发 portal 截图对比后发现:
- Client ID 第 8 字符:`REAH` vs `rEAH`(大写 R 应小写 r)
- Client Secret 第 9 字符:`HI66` vs `H166`(大写 I 应数字 1)
- 两处都是**典型 0/O、I/1/l 视觉混淆**

server 收到错的凭证,鉴权失败统一报 `CH_CLIENT_AUTH_FAILURE / wrong random id`。

**修复**: 改 `.env` 把 `REAH` → `rEAH`、`HI66` → `H166`。改完跑 `python scripts/ctrader_poc.py --skip-bars` 验证。

**教训**:
1. cTrader portal 复制 clientId/secret 一定要脚本对比,不能肉眼看(`secrets-tool` 之类的 diff 工具)
2. `wrong random id` 是误导性错误描述,实际几乎都是凭证错
3. 真要 lazy 一点:用 `os.environ.get(...)` 出来后立即 `assert == EXPECTED_HASH` 做完整性校验


### Finding 1 — `clientMsgId` 是 wire 协议字段,不是 callback key

`tcpProtocol.py:42-45`:
```python
if isinstance(message, ProtoMessage.__base__):
    msg = ProtoMessage(payload=message.SerializeToString(),
                       clientMsgId=clientMsgId,
                       payloadType=message.payloadType)
    data = msg.SerializeToString()
```

**含义**: `clientMsgId` 进了 `ProtoMessage` 容器(序列化上 wire),跟 `Client._responseDeferreds` 那个 callback map key 是同一个值但作用在不同层。SDK 默认 `str(id(responseDeferred))`(Python 内存地址),这个字符串会原封不动过 wire。

**实测**: 默认 id=`2752142768704` 被拒;UUID `e4922bdf-...` 也被拒 — 证明 `clientMsgId` 格式不是问题。

### Finding 2 — 官方 sample 根本不传 `clientMsgId`

`OpenApiPy_index.md` 第 44-48 行 + 第 53-54 行:
```python
applicationAuthReq = ProtoOAApplicationAuthReq()
applicationAuthReq.clientId = "Your App Client ID"
applicationAuthReq.clientSecret = "Your App Client secret"
...
deferred = client.send(applicationAuthReq)   # 不传 clientMsgId
```

官方 sample 没设 → SDK 默认 `str(id(deferred))` → 必拒。说明官方 sample 跑不通(或者 sample 写的 host 不是 demo)。

### Finding 3 — `ProtoOAApplicationAuthReq` 只有 3 个字段

`spa_messages.html` 字段表:
| Field | Type | Label | Description |
|---|---|---|---|
| `payloadType` | ProtoOAPayloadType | Optional | (消息类型枚举,wire 上由 `ProtoMessage.payloadType` 带,不靠这个) |
| `clientId` | string | **Required** | 注册时给 |
| `clientSecret` | string | **Required** | 注册时给 |

就这俩。没有任何 clientMsgId 字段。

### Finding 4 — `errorCode` 是 `ProtoErrorCode` 或 `ProtoCHErrorCode` 名字

`spa_messages.html` / `spa_error-handling.html` 反复强调:
> `errorCode: The name of the ProtoErrorCode or the other custom ErrorCodes (e.g. ProtoCHErrorCode).`

**`CH_CLIENT_AUTH_FAILURE` 是 `ProtoCHErrorCode`**(Cluster Handler 内部错误),不在通用 ProtoErrorCode 里。完整列表需要去 `_pb2.py` 反查。

### Finding 5 — 客户端 sample 用 OAuth2 全套,不是 access token 直连

`authentication.md` 描述的标准流程是:
1. `getAuthUri()` → 用户浏览器去 https://connect.spotware.com/apps/auth?client_id=...&redirect_uri=...&scope=trading
2. 用户授权后 callback 带 `?code=xxx`
3. `getToken(code)` POST 到 https://connect.spotware.com/apps/token → 拿 `accessToken` + `refreshToken`
4. `clientMsgId` 任意(SDK 默认就够)

**我们当前 `.env` 里直接 hardcode 了 `CTRADER_ACCESS_TOKEN`** — 跳过了 OAuth2 流程,但 spotware 的 demo token 应该可以直连。如果 token 过期或被 revoke,App auth 会 `CH_CLIENT_AUTH_FAILURE` 报"wrong random id"只是 server 给的模糊描述。

## 对 `wrong random id` 修复的猜测(实测后已修正)

~~1. 最可能: CTRADER_ACCESS_TOKEN 过期/无效~~
~~2. 次可能: CTRADER_CLIENT_ID 跟 token 不匹配~~
~~3. 次可能: clientMsgId 格式问题~~

实测排除了上面三个。**真实根因候选(按可能性排序):**
1. **`clientId` 是 live 环境(对应 live host `live.ctraderapi.com`),我们连了 demo host** — Pepperstone 给的 clientId 通常绑 host
2. **`clientId` 在 Pepperstone portal 被 disable / 还在审核状态** — admin 拒绝
3. **App 注册时 `redirect_uri` 配错** — OAuth flow 必填字段,app auth 阶段也会校验
4. **access_token 跟 clientId **不匹配**(同一 app 重新注册过,新 secret 没刷进 `.env`) — server 比对 token 签发时的 clientId

## 下一步(待用户决定)

- [ ] 跑 `scripts/ctrader_oauth.py` 走完整 OAuth 流(看能不能拿到新 token)
- [ ] 试改 host 为 `live.ctraderapi.com:5035` 看错误是否变(排除 live/demo 错配)
- [ ] 把 `CTRADER_CLIENT_ID` 拿到 https://connect.spotware.com 看 app 状态
- [ ] 实在不行:走 `ProtoOAVersionReq`(无认证,纯握手) 看 server 会不会给更详细的 connect 错误

## 用户如何用这堆材料

```bash
# 1. 验证我抓的东西
ls -la data/charts/ctrader_docs/
cat data/charts/ctrader_docs/INDEX.md   # 本文件

# 2. 关键搜索
grep -n "clientMsgId" data/charts/ctrader_docs/{client.py,tcpProtocol.py}
grep -n "ProtoCHErrorCode\|CH_CLIENT_AUTH" data/charts/ctrader_docs/spa_*.html
grep -n "ProtoOAApplicationAuthReq" data/charts/ctrader_docs/{OpenApiPy_index.md,spa_messages.html}

# 3. 视觉参考
ls -la data/charts/ctrader_docs/screenshot_messages.png
```

## 待办(本次不做,留给下一轮)

- [ ] 验证 `CTRADER_ACCESS_TOKEN` 有效期(用 scripts/ctrader_oauth.py 重新跑 OAuth 流)
- [ ] 拉 `OpenApiModelMessages_pb2.py` 反查 `ProtoCHErrorCode` 完整定义,看 `CH_CLIENT_AUTH_FAILURE` 真因描述
- [ ] 改 `ctrader_bridge.py:239` 那条 stale 注释(声称 `str(uuid.uuid4())` 修了 wrong random id,实际不修)
- [ ] 修 `client.py` constructor — 当前我们没传 `numberOfMessagesToSendPerSecond=5`(其实默认就是 5,这条不修)
