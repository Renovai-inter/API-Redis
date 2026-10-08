import os

import redis


def conectar_redis():
    """Usa a mesma conexão configurada na API e no sincronizador."""
    for url in ([os.getenv('REDIS_URL')] if os.getenv('REDIS_URL') else []) + [None]:
        client = None
        try:
            options = dict(protocol=2, decode_responses=True,
                           socket_connect_timeout=5, socket_timeout=5)
            if url:
                client = redis.Redis.from_url(url, **options)
            else:
                client = redis.Redis(
                    host=os.getenv('REDIS_HOST', 'localhost'),
                    port=int(os.getenv('REDIS_PORT', '6379')),
                    password=os.getenv('REDIS_PASSWORD') or None,
                    **options,
                )
            client.ping()
            return client
        except (redis.RedisError, ValueError):
            if client is not None:
                client.close()
            print('Falha ao conectar via REDIS_URL; tentando Redis local.' if url
                  else 'Erro ao conectar Redis local.')
    return None
