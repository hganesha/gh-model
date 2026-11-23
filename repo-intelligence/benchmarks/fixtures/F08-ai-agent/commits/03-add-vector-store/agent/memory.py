import chromadb

collection = chromadb.PersistentClient(path=".chroma").get_or_create_collection("tickets")


def recall(question: str) -> list[str]:
    return collection.query(query_texts=[question], n_results=3)["documents"][0]
