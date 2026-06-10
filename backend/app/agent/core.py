import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from app.tools.dictionary import search_term_dict
from app.tools.wiki_lookup import lookup_wiki
from app.translation_prompts import AGENT_ASR_SYSTEM_RULES

PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()

# 防死循环相关参数（可通过 .env 覆盖）
AGENT_MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "5"))
AGENT_MAX_EXECUTION_TIME = float(os.environ.get("AGENT_MAX_EXECUTION_TIME", "60"))

def get_agent_executor():
    # 从你的 .env 环境变量读取 KEY
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL", None)

    # 简单识别：如果是用来调用 DeepSeek 的，用 deepseek-chat；不然兜底回 gpt-3.5-turbo
    model_name = "deepseek-chat" if (base_url and "deepseek" in base_url.lower()) else "gpt-3.5-turbo"

    llm = ChatOpenAI(
        api_key=api_key,
        base_url=base_url if base_url else None,
        model=model_name,
        streaming=True
    )

    # 给大模型装配上“武器”
    tools = [search_term_dict, lookup_wiki]

    # ReAct / Tool Calling Prompt，教大模型如何思考
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一个精通垂直领域的智能翻译 Agent。你的任务是将用户的外语（通常是日语）翻译成地道、专业的中文。\n"
                   "【规则】\n"
                   "1. 若用户消息中含「专名术语表」，表中词条必须按推荐译法处理，禁止意译为日常含义。\n"
                   "2. 遇到术语表未覆盖的疑难名词、领域黑话、专有术语，先调用 search_term_dict 工具检索本地术语库。\n"
                   "3. 若本地术语库返回「未找到」，或需核实 VTuber/游戏/ACG 专名译名与背景，再调用 lookup_wiki 查询萌娘百科/维基百科。\n"
                   "3a. lookup_wiki 查询 ACG/手游角色、昵称、组合名时：source 优先用 moegirl（或 auto，工具会按视频背景自动选源）；"
                   "query 应使用角色昵称或日文全名，避免单独搜索泛词（如「ゲーム」「攻略」）；"
                   "示例：查「ヤン子」→ query=\"ヤン子\" source=\"moegirl\"；不要 query=\"PEAK ゲーム\" source=\"ja_wiki\"。\n"
                   "3b. 若 Wiki 返回「均未找到」，可换 query 再搜一次（如去掉泛词、改用假名全名），"
                   "但同一字幕行最多调用 lookup_wiki 2 次，禁止编造译名。\n"
                   "4. 工具仅在后台使用，最终面向用户的输出只能是翻译结果本身；禁止出现「查一下」「萌娘百科」「维基」等过程描述。\n"
                   "5. 禁止「原文→译文」对照格式；不要生硬机翻，结合术语表与工具查到的意思输出信达雅的中文；禁止编造组织归属或人名。\n"
                   f"{AGENT_ASR_SYSTEM_RULES}"),
        ("placeholder", "{chat_history}"),
        ("user", "{input}"),
        ("placeholder", "{agent_scratchpad}"),
    ])

    agent = create_tool_calling_agent(llm, tools, prompt)

    # AgentExecutor 负责 ReAct 循环与防死循环控制：
    # - max_iterations: 限制“思考->调用工具”的最大轮数，杜绝无限工具调用循环
    # - max_execution_time: 单次请求最长执行时间(秒)，超时强制结束
    # - handle_parsing_errors: 模型输出无法解析时不崩溃，返回提示让其自我修正
    # - early_stopping_method="force": 触发上限时强制收尾，返回当前已有结果
    return AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        max_iterations=AGENT_MAX_ITERATIONS,
        max_execution_time=AGENT_MAX_EXECUTION_TIME,
        handle_parsing_errors=True,
        early_stopping_method="force",
    )

agent_executor = get_agent_executor()