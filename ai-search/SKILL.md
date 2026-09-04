---
name: ai-search
description: AI 联网搜索,默认 ai+tavily 双路并发(AI 模型联网搜索 + Tavily 拿权威 URL,互相补强),配合 --split 多维度拆分。Exa 用于找论文/博客深度内容。不仅当用户提到搜索、查询最新内容、调研、需要联网信息时务必使用,当 AI 模型判断回答当前问题需要最新知识或信息时,也应主动调用此 skill。默认用 ai+tavily 并发并加 --split 3~5。
---

# AI Search Skill

通过 `~/.claude/skills/ai-search/ai_search.py` 进行联网搜索。**默认策略: `ai+tavily` 双路并发 + `--split` 拆分,两路结果在输出里各占一段,互相补强。**

| Provider | 角色 | 何时用 |
|---------|------|--------|
| `ai` | 联网搜索 + 直接回答 | 模型自带联网,能整合并直接产出回答 |
| `tavily` | 拿权威 URL、可信来源 | 专业搜索引擎,带链接和摘要,经 AI 整合后再返回 |
| `exa` | 论文、博客、长文深度内容 | 神经搜索,适合"找一篇关于 X 的文章/论文"的场景 |

**默认 `ai+tavily` 并发**: 一个负责回答,一个负责来源,Claude 在外层把两边的结论 + 链接融合给用户。

## 触发条件

**核心原则: 凡是可能有"最新"信息的场景,一律先搜再答,不做"据我所知"的猜测。此外,AI 模型在回答时应自行判断: 当前问题是否涉及可能在训练数据截止后发生变化的信息? 如果是,主动调用此 skill。**

### 必须搜索的场景

- **AI 自主判断**: 回答任何问题时,先评估答案是否依赖可能过期的知识(如版本号、API、价格、状态、新闻等)。只要存在不确定性,主动搜索而非凭训练记忆回答。
- **用户显式调用 `/ai-search`**
- **用户明确提到搜索、查询、调研、联网等关键词**
- **时效性问题**: 新闻、动态、事件、股价、价格、版本号、发布日期
- **技术问题(尤其 AI/大模型)**: AI 领域迭代极快,模型能力、API 价格、框架版本、最佳实践可能每月都在变。涉及以下关键词时务必搜索:
  - 大模型、LLM、AI、Agent、MCP、RAG、向量数据库、Prompt Engineering
  - 框架/库的最新用法: LangChain、LlamaIndex、CrewAI、AutoGen、Dify 等
  - 模型能力对比: GPT-5、Claude 4、Gemini 3、DeepSeek 等
  - AI 工具/SaaS: Cursor、Copilot、Windsurf、Bolt 等
- **技术方案/架构设计**: 用户让生成技术方案、选型建议、架构设计时,先搜索业界最新实践和方案,而非仅凭经验
- **代码生成/开发任务**: 用户让写代码用到某个库/框架时,先搜最新 API 和 breaking changes,避免给出过期写法
- **需要多维度搜索的复杂查询**: 调研、对比、深度分析
- **论文、技术文档、博客深度内容查找**: 用 `--provider exa`
- **事实核查、找权威来源**
- **用户显式调用 `/ai-search`**

### 反例

- ❌ 用户问"React 19 有哪些新特性" — 直接凭记忆回答,可能漏掉最新 minor 版本更新
- ❌ 用户问"用 LangChain 写个 agent" — 直接生成代码,可能 API 已过时
- ❌ 用户问"设计一个 RAG 系统" — 直接写方案,不提最新的 Chunking 策略或向量库选型
- ❌ 用户问"GPT-5 和 Claude 4 哪个好" — 凭印象对比,不查最新 benchmark

### 可以不搜索的例外

- 纯逻辑/数学问题(算法复杂度、数学证明)
- 基础语法问题(某个 Python 内置函数的参数,不会变的东西)
- 用户明确说"不需要搜索"

## 使用方式

```bash
# 默认推荐: ai+tavily 双路并发 + 拆分 (绝大多数场景)
python3 ~/.claude/skills/ai-search/ai_search.py --split 4 "查询内容"

# 等同于显式写法
python3 ~/.claude/skills/ai-search/ai_search.py --provider ai+tavily --split 4 "查询内容"

# 极简单的单一事实题 — 不拆分,但仍走 ai+tavily 双路
python3 ~/.claude/skills/ai-search/ai_search.py "Python 最新版本号"

# 三路全开 (深度调研场景)
python3 ~/.claude/skills/ai-search/ai_search.py --provider ai+tavily+exa --split 5 "船舶推进器行业 2026 趋势"

# 只用单 provider (节省调用)
python3 ~/.claude/skills/ai-search/ai_search.py --provider ai --split 4 "查询内容"

# 找论文/博客直接 exa
python3 ~/.claude/skills/ai-search/ai_search.py --provider exa "查询内容"

# JSON 格式输出 (便于解析)
python3 ~/.claude/skills/ai-search/ai_search.py --json --split 4 "查询内容"

# 从 stdin 读取查询
echo "查询内容" | python3 ~/.claude/skills/ai-search/ai_search.py --read-stdin --split 4
```

**provider 组合写法**: `--provider` 支持 `+` / `,` / 空格 任一分隔,如 `ai+tavily`、`ai,tavily`、`ai tavily` 都等价。重复的会自动去重。

## 搜索策略

**核心原则: 默认 `ai+tavily` 并发 + 拆分,只有最简单的单一事实题才考虑减项。**

实测下来,`ai+tavily` 并发比单跑 `ai` 准确度更高、来源更可信;再配 `--split`,每条子查询都让两路同时打,信息覆盖度最佳。

### 拆分粒度速查表

| 查询类型 | `--split` 值 | 例 |
|----------|-------------|-----|
| 单一事实题 (一句话能问完) | 不加 `--split` | "Python 最新版本""今天北京天气" |
| 普通信息查询 | `--split 3` | "OpenAI 最近发布了什么""React 19 有哪些新特性" |
| 调研、对比、综合分析 | `--split 4~5` | "对比 Rust 和 Go 的并发模型""调研下国内做船舶推进器的厂商" |
| 行业情报、深度研究 | `--split 5~6` | "船舶推进器行业 2026 年趋势" |
| 再多就分散了,不要超 6 | — | — |

### Provider 组合速查表

| 场景 | provider 组合 |
|------|--------------|
| 默认(几乎所有调研、新闻、综合查询) | `ai+tavily` (默认,可省略) |
| 节省调用(只要 AI 整合即可) | `ai` |
| 仅需权威 URL,不要 AI 自带联网 | `tavily` |
| 找论文/博客/深度文章 | `exa` |
| 深度行业研究(三路全开) | `ai+tavily+exa` |
| 二次核实、交叉验证 | `tavily+exa` |

### 整合给用户

`ai+tavily` 的输出分为 `# 来源: ai` 和 `# 来源: tavily` 两段。Claude 应:
- 用 ai 段作为主要事实/结论
- 用 tavily 段补充可信 URL 和最新动态
- 用自然语言融合成一份回答给用户,引用关键链接
- **不要直接 dump 两段原始 markdown**

**反例 (不要这样做)**:
- ❌ 用户问"调研 X" 却没加 `--split`,导致回答泛泛而谈
- ❌ 单一事实题硬上 `--split 5`,反而增加延迟
- ❌ 简单问答硬上 `ai+tavily+exa`,浪费 token
- ❌ 用户要"找论文"却用 ai 而非 exa

## 配置

配置文件位于 `~/.ai-search-mcp/config.json`:

```json
{
  "api_url": "http://your-api:10000",
  "api_key": "your-api-key",
  "search_model_id": "用于 ai provider 联网搜索的模型",
  "analysis_model_id": "用于拆查询和整合 tavily/exa 结果的模型(可选,缺省则用 search_model_id)",
  "analysis_api_url": "可选: 拆分/整合走独立 endpoint;留空或不写则复用主 api_url",
  "analysis_api_key": "可选: 同上,留空则复用主 api_key",
  "default_provider": "ai+tavily",

  "tavily_api_url": "https://api.tavily.com/search",
  "tavily_api_key": "tvly-xxx",
  "tavily_search_depth": "advanced",
  "tavily_max_results": 8,
  "tavily_include_answer": true,
  "tavily_timeout": 90,
  "tavily_concurrency": 2,

  "exa_api_url": "https://api.exa.ai/search",
  "exa_api_key": "xxx",
  "exa_search_type": "auto",
  "exa_num_results": 8,
  "exa_text_max_chars": 1500,

  "timeout": 60,
  "analysis_timeout": 90,
  "stream": true,
  "filter_thinking": true,
  "search_retry_count": 1,
  "analysis_retry_count": 1
}
```

`default_provider` 支持组合写法(`ai+tavily`),也支持单 provider(`ai`)。

**注意**:Tavily 和 Exa 的 API key 都通过它们的官方控制台获取(tavily.com、exa.ai)。AI 整合环节默认复用主 `api_url`/`api_key`/`analysis_model_id`;若想让搜索模型和整合模型分别走不同服务(比如搜索用 Grok,整合用 Claude),填上 `analysis_api_url`/`analysis_api_key` 即可。

也支持环境变量:`AI_API_URL`、`AI_API_KEY`、`AI_ANALYSIS_API_URL`、`AI_ANALYSIS_API_KEY`、`TAVILY_API_KEY`、`EXA_API_KEY`、`AI_PROVIDER` 等。

## 错误处理

脚本内部已有重试机制。如果返回错误:

- **`[错误] 未配置 tavily_api_key/exa_api_key`** → 引导用户去对应平台申请并填入 config
- **`[错误] Tavily 401 / Exa 401`** → API key 无效或额度耗尽
- **`[错误] 认证失败`** → 检查 `api_key`(AI 整合用的)
- **`超时`** → 建议降低查询复杂度或增大 `timeout`
- **`配置缺失`** → 引导用户创建 `~/.ai-search-mcp/config.json`
- **`⚠️ tavily 源暂不可用, 已跳过该来源`** → 第三方 Tavily 中转站在 `--split` 并发下返 554/超时, 已自动降级, ai 路结果仍可用; 可调大 `tavily_timeout` 或调小 `tavily_concurrency`

## 结果解读

- **单 provider**: 直接返回 AI 整合后的 Markdown 回答;`--split` 时分多个 `### 子查询 N` 段
- **多 provider 组合** (如 `ai+tavily`): 输出按 `# 来源: <provider>` 分段,中间用 `---` 分隔,各段独立完整
- Claude 应:
  - 多 provider 时把各段的结论 + URL 融合成自然语言回复,**不要直接 dump 各段原始 markdown**
  - 优先把关键 URL/事实核查后的结论转述给用户
