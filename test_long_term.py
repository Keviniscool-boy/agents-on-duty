import asyncio

from memory.long_term import LongTermMemory


async def main():
    memory = LongTermMemory()

    # 第一次运行时读取知识库并保存索引。
    if not memory.documents:
        count = await memory.load_knowledge_base("knowledge_base")
        memory.save()
        print(f"首次加载并保存文档数量: {count}")
    else:
        print("检测到已有持久化知识库，跳过重复加载")

    # 重新创建对象，模拟程序重启后的恢复过程。
    restored_memory = LongTermMemory()

    print(f"恢复后的文档数量: {len(restored_memory.documents)}")
    print(f"恢复后的向量数量: {restored_memory.index.ntotal}")

    results = await restored_memory.search(
        "签收后几天可以退货？",
        top_k=3,
    )

    print("恢复后的搜索结果:")
    for result in results:
        print(result)


asyncio.run(main())
