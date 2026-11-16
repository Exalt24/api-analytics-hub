"""Technology and skill names the tailoring loop treats as claims.

A resume can invent a tool in lower case just as easily as in capitals ("kubernetes", "terraform"), and a capitalisation test alone would
let it through. So every term here that appears in a generated document must also appear in the applicant's own facts (tailor.grounding).
The same list is the vocabulary the ATS score reads out of a job post. It is a list of things people claim, not a list of things that exist:
a term missing from it is still caught when it is capitalised or carries a digit (see tailor.entity_tokens), which is why both checks run.
Words that are also plain English (go, make, rest, express, render, spark, swift, lever) are left out on purpose: they would flag honest prose.
"""
from __future__ import annotations

TERMS = frozenset("""
python javascript typescript java kotlin golang rust ruby php scala c++ c# sql nosql bash powershell matlab
node.js nodejs react next.js nextjs vue angular svelte django flask fastapi nestjs rails laravel graphql grpc websocket
postgresql postgres mysql sqlite mongodb redis memcached elasticsearch opensearch cassandra dynamodb snowflake bigquery redshift databricks
kafka rabbitmq sqs sns pubsub celery bullmq dagster prefect dbt n8n zapier
docker kubernetes helm terraform ansible pulumi jenkins github gitlab circleci aws gcp azure cloudflare vercel heroku railway netlify
supabase firebase qdrant pinecone weaviate chroma pgvector faiss langchain llamaindex langgraph openai anthropic gemini groq ollama huggingface pytorch tensorflow
playwright puppeteer selenium cypress jest pytest stagehand browserbase appsmith retool notion airtable
linux nginx caddy traefik prometheus grafana sentry datadog opentelemetry
stripe twilio sendgrid shopify salesforce hubspot zoho quickbooks slack
greenhouse workday smartrecruiters icims taleo
oauth jwt saml sso rbac
llm rag embeddings
""".split())
