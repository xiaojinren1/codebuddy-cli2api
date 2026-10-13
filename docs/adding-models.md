# 新增模型指南

> **一句话**：往 [`models.extra.json`](../models.extra.json) 的 `models` 数组里加一条，保存，下一步请求就生效。
> 不用重启网关，不用等缓存过期，不用改 Python 代码。

---

## 1. 为什么需要这个文件

网关的模型列表来自 **CodeBuddy CLI 缓存的目录**，而不是上游服务端。这两者经常不同步：

- 上游服务端早就支持的模型，CLI 的目录里可能几周都没有
- 有些模型（例如第三方模型）**永远不会**出现在 CLI 目录里

真实案例：`deepseek-v4.1-flash` 在本机已被调用过 **11919 次**（说明上游完全支持），但因为它不在 CLI 目录里，`/v1/models` 里根本看不到，客户端也就选不到。

`models.extra.json` 就是为了填这个缺口。

---

## 2. 模型目录是怎么解析出来的

### 2.1 主目录（四级，取第一个非空的）

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `CODEBUDDY_MODELS_URL` | 显式远程 JSON。默认未设置 |
| 2 | `~/.codebuddy/local_storage` | CLI 的账号级缓存目录，**通常走这条** |
| 3 | CLI 自带 `product.json` | 通用默认值 |
| 4 | 代码里的 `MODELS` 常量 | 兜底 |

### 2.2 补充层（主目录缺什么补什么）

```
主目录（上面四级）
    ↓ 缺的才补
models.extra.json          ← 你平时改这个
    ↓ 还缺才补
代码里的 _CATALOG_EXTRAS    ← 保底，一般不用动
```

**按 id 去重**：某一层已经有了，后面的层就跳过。所以 CLI 哪天自己收录了某个模型，你 JSON 里的条目会自动让位，**不用回来清理**。

### 2.3 缓存与热加载

目录结果默认缓存 1 小时（`CODEBUDDY_MODELS_TTL`）。但有一个例外：

> **只要 `models.extra.json` 的修改时间变了，缓存立即失效。**

这就是"改完保存即生效"的实现方式，也是为什么加模型不需要重启网关。

---

## 3. 我需要做什么？

| 情况 | 要做的事 |
|---|---|
| CLI 更新后新模型自动出现 | **什么都不用做**，等 1 小时 TTL 或重启网关 |
| 上游能用，但 `/v1/models` 里没有 | 按第 4 节加进 JSON |
| 想让某模型在 Codex 里有推理档位菜单 | 加 JSON **并且**改一行代码，见第 6 节 |
| 想改某个已存在模型的属性（如视觉支持） | 改不了，主目录优先级更高 |

---

## 4. 添加模型：三步

### 第 1 步：先验证 slug 能不能跑通

**这一步不要跳过。** 写错的 id 加进去也没用，只会让目录里多一个选不了的模型。

```bash
curl -s localhost:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"<候选ID>","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

判断结果：

| 返回 | 含义 |
|---|---|
| `HTTP 200` | ✅ slug 正确，继续第 2 步 |
| `HTTP 400` + `model [xxx] service info not found` | ❌ slug 不对，换个写法再试 |

**命名陷阱**：上游的 slug 不一定是你在界面上看到的名字。

- 界面上显示 `Step-5-Preview Medium`，真实 slug 是 **`step-5-preview`**
  （`step-5`、`step5`、`step-5-medium`、`Step-5-Preview` 全部返回 400；`Medium` 是档位标签，不属于 id）
- 界面上显示 `Hy4 preview High`，真实 slug 是 **`hy4-preview`**

**怎么找 slug**：优先看网关日志里客户端实际发出的 model 字段，或在 CodeBuddy 客户端里选一次该模型，看请求里的值。

### 第 2 步：加进 JSON

编辑仓库根目录的 [`models.extra.json`](../models.extra.json)，往 `models` 数组追加一条：

```json
{
  "id": "新模型ID",
  "name": "显示名",
  "supports_vision": true,
  "max_output_tokens": 48000,
  "reasoning": { "effort": "max" }
}
```

### 第 3 步：验证

```bash
curl -s localhost:8787/v1/models | python3 -c \
  "import json,sys; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

新 id 应该立即出现在列表里。**不需要重启网关。**

再发一次真实请求确认能出内容（注意 `max_tokens` 给足，见第 7 节）：

```bash
curl -s localhost:8787/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"新模型ID","messages":[{"role":"user","content":"只回复：正常"}],"max_tokens":2000}'
```

---

## 5. 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 上游 slug，**必须完全一致** |
| `name` | | 显示名，缺省用 `id` |
| `supports_vision` | | 是否接受图片输入，默认 `false` |
| `max_output_tokens` | | 输出上限，一般填 `48000` |
| `reasoning` | | **推理模型才填**，如 `{"effort": "max"}`；非推理模型**整条省略** |
| `description` | | 可选备注，会出现在列表里 |

也接受上游目录的原生字段名，两者等价：

| 本文件写法 | 等价写法 |
|---|---|
| `supports_vision` | `vision` / `supportsVision` / `img` |
| `max_output_tokens` | `maxOutputTokens` / `max_out` / `maxOut` |
| `context_window` | `contextWindow` / `max_context_window` |

---

## 6. 让模型在 Codex 里出现推理档位菜单

**这是唯一需要改代码的场景。**

JSON 里的 `"reasoning"` 只设置**默认 effort**；要在 Codex 的模型选择器里出现 effort 档位菜单（low / medium / high / xhigh），需要在 `codebuddy_gateway/common.py` 的 `_CODEX_REASONING_LEVELS` 里加一行：

```python
_CODEX_REASONING_LEVELS = {
    ...
    "新模型ID": (["low", "medium", "high", "xhigh"], "xhigh"),
}
```

改完需要**重启网关**（这是代码，不是数据文件）。

> 为什么分开？数据文件描述"上游有什么模型"，代码描述"客户端该怎么呈现"。混在一起会让 JSON 承担它不该承担的语义。

---

## 7. 常见问题

### 加了模型，但列表里没有

1. 确认改的是**仓库根目录**的 `models.extra.json`，不是 `codebuddy_gateway/` 里的
2. 确认 JSON 语法合法：`python3 -m json.tool models.extra.json`
3. 看网关日志有没有 `model supplement` 相关的告警

### 模型能选，但回复是空的

**推理模型 + `max_tokens` 太小**。推理模型会先消耗 token 思考，额度不够时 `content` 返回 `null`，思考内容在 `reasoning_content` 字段里。

给足 `max_tokens`（建议 ≥ 2000）即可。这不是配置问题。

### JSON 写坏了会怎样

**服务不会挂。** 会记录一条告警，然后降级使用代码兜底常量：

```
model supplement unreadable (...): Expecting value: line 5 column 1
```

单个条目缺 `id` 时只跳过那一条，其余照常加载。

### 怎么知道上游出新模型了

没有自动通知。几个线索：

- 关注模型厂商的发布信息
- 客户端（CodeBuddy / WorkBuddy）里出现了新的选择项
- 网关日志里出现某个没见过的 model 名
- 定期看 `~/.cc-switch` 的请求日志，客户端有时会先用到

发现后按第 4 节三步走即可。

### 模型不会被重复添加吗

不会。三层各自按 id 去重，且优先级是"主目录 > JSON > 代码常量"。同一个 id 只会出现一次。

---

## 8. 附录

### 相关文件

| 路径 | 作用 |
|---|---|
| `models.extra.json` | **你平时改这里** |
| `codebuddy_gateway/common.py` | `_CODEX_REASONING_LEVELS` 在这里 |
| `.env.example` | 环境变量说明 |

### 相关环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEBUDDY_MODELS_EXTRA_FILE` | `./models.extra.json` | 补充文件位置。设为空值可禁用该层 |
| `CODEBUDDY_MODELS_TTL` | `3600` | 主目录缓存秒数（补充文件不计入缓存判断） |
| `CODEBUDDY_MODELS_URL` | — | 显式远程目录，优先级最高 |

### 判断依据速查

```bash
# 当前生效的模型列表
curl -s localhost:8787/v1/models | python3 -m json.tool

# 验证某个 slug 是否可用
curl -s localhost:8787/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<ID>","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'

# 校验 JSON 语法
python3 -m json.tool models.extra.json > /dev/null && echo OK
```
