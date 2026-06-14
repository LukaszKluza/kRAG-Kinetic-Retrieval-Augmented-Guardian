import os

import chromadb
from chromadb.config import Settings
import requests
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")       # locally
# OLLAMA_URL = "http://ollama.ollama.svc.cluster.local:80"  # in cluster

EMBED_MODEL = "nomic-embed-text"
CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", 8000))
# CHROMA_HOST = "chromadb.chromadb.svc.cluster.local"  # in cluster


def get_chroma_client() -> chromadb.HttpClient:
    return chromadb.HttpClient(
        host=CHROMA_HOST,
        port=CHROMA_PORT,
        settings=Settings(anonymized_telemetry=False),
    )


def get_or_create_collection(name: str = "krag_incidents"):
    client = get_chroma_client()
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def embed(text: str) -> list[float]:
    response = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": EMBED_MODEL, "prompt": text},
    )
    response.raise_for_status()
    return response.json()["embedding"]


def store_incident(
    problem: str,
    solution: str,
    metadata: dict | None = None,
) -> str:
    collection = get_or_create_collection()
    doc_id = f"incident_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    text = f"PROBLEM: {problem}\nSOLUTION: {solution}"

    collection.add(
        ids=[doc_id],
        embeddings=[embed(text)],
        documents=[text],
        metadatas=[{
            "type": "incident",
            "timestamp": datetime.utcnow().isoformat(),
            **(metadata or {}),
        }],
    )
    logger.info(f"Stored incident: {doc_id}")
    return doc_id


def search_similar_incidents(alert_description: str, n_results: int = 3) -> list[dict]:
    collection = get_or_create_collection()
    count = collection.count()
    if count == 0:
        logger.info("No incidents found in the database — clean history")
        return []

    results = collection.query(
        query_embeddings=[embed(alert_description)],
        n_results=min(n_results, count),
        include=["documents", "distances", "metadatas"],
    )

    return [
        {
            "document": doc,
            "distance": dist,
            "metadata": meta,
        }
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        )
    ]


def ingest_runbook(title: str, content: str, source: str = "manual") -> str:
    collection = get_or_create_collection("krag_runbooks")
    doc_id = f"runbook_{title.lower().replace(' ', '_')}"

    collection.upsert(
        ids=[doc_id],
        embeddings=[embed(content)],
        documents=[content],
        metadatas=[{"title": title, "source": source, "type": "runbook"}],
    )
    logger.info(f"Loaded runbook: {title}")
    return doc_id


def search_runbooks(query: str, n_results: int = 2) -> list[dict]:
    collection = get_or_create_collection("krag_runbooks")
    if collection.count() == 0:
        return []

    results = collection.query(
        query_embeddings=[embed(query)],
        n_results=min(n_results, collection.count()),
        include=["documents", "metadatas"],
    )
    return [
        {"document": doc, "metadata": meta}
        for doc, meta in zip(
            results["documents"][0],
            results["metadatas"][0],
        )
    ]
