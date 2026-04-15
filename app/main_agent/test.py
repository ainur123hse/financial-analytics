import asyncio
from app.main_agent.run import answer_to_question
from pathlib import Path

res = asyncio.run(answer_to_question("какая динамика по выручке у магазинов у дома ленты?", Path("markdowns")))
print(res)
