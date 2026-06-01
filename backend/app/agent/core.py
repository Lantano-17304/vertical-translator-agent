import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.prompts import ChatPromptTemplate
from app.tools.dictionary import search_term_dict

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
    tools = [search_term_dict]

    # ReAct / Tool Calling Prompt，教大模型如何思考
    prompt = ChatPromptTemplate.from_messages([
        ("system", "你是一个精通垂直领域的智能翻译 Agent。你的任务是将用户的外语（通常是日语）翻译成地道、专业的中文。\n"
                   "【规则】\n"
                   "1. 遇到疑难名词、领域黑话、专有术语等，你*必须*调用 search_term_dict 工具进行检索确认。\n"
                   "2. 不要生硬机翻，结合工具查到的意思，输出信达雅的最终翻译翻译结果。"),
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