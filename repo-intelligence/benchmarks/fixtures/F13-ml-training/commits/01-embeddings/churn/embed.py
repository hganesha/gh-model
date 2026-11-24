from sentence_transformers import SentenceTransformer

encoder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")


def embed(texts):
    return encoder.encode(texts)
