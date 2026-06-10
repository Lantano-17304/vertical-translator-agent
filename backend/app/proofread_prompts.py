"""校对 Agent prompt（无工具，仅复审初译）。"""

PROOFREAD_SYSTEM = (
    "你是日语游戏/ACG 字幕校对专家。输入为「原文 + 初译」，你的任务是输出修订后的中文字幕。\n"
    "【规则】\n"
    "1. 专名、术语表词条必须与推荐译法一致，全文统一写法；\n"
    "2. 修正 ASR 误听导致的语义错误，勿照抄错字含义；\n"
    "3. 删除「查一下」「萌娘百科」「维基」等过程描述，只保留观众可见字幕；\n"
    "4. 保持口语化、适合字幕长度；禁止保留日文假名；\n"
    "5. 若初译已正确，原样输出，不要为改而改；\n"
    "6. 禁止输出解释、对照箭头、Markdown。"
)


def proofread_line_user(
    source: str,
    draft: str,
    *,
    prev_line: str = "",
    next_line: str = "",
) -> str:
    ctx = ""
    if prev_line.strip() or next_line.strip():
        ctx = (
            f"上一句：{prev_line.strip() or '（无）'}\n"
            f"下一句：{next_line.strip() or '（无）'}\n\n"
        )
    return (
        f"{ctx}"
        f"原文：{source}\n"
        f"初译：{draft}\n\n"
        "请只输出修订后的一行中文译文。"
    )


def proofread_batch_user(pairs: list[tuple[str, str]]) -> str:
    numbered = "\n".join(
        f"[{i}] 原文：{src}\n    初译：{draft}"
        for i, (src, draft) in enumerate(pairs, start=1)
    )
    return (
        "下面是带行号的字幕原文与初译，请逐行校对。\n"
        "严格要求：\n"
        "1. 每行输出格式为 [序号] 修订译文；\n"
        "2. 输出行数必须与输入完全一致，禁止合并/拆分/增删行；\n"
        "3. 只输出修订结果，不要解释。\n\n"
        f"{numbered}"
    )
