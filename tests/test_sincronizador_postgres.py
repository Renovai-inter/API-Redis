"""Integração: viagem pela API (Redis real) -> finalizar -> sincronizador grava no Postgres real.

Só roda com REDIS_URL_TESTE e DATABASE_URL_TESTE apontando para servidores descartáveis, ex.:
    docker run -d -p 6379:6379 redis:7-alpine
    docker run -d -p 55432:5432 -e POSTGRES_PASSWORD=teste -e POSTGRES_DB=renovai_teste postgres:16-alpine
    REDIS_URL_TESTE=redis://localhost:6379/15 \
    DATABASE_URL_TESTE=postgresql://postgres:teste@localhost:55432/renovai_teste pytest tests
"""
import os
from pathlib import Path
import runpy
import unittest
import uuid
from unittest.mock import MagicMock, patch

REDIS_URL = os.getenv('REDIS_URL_TESTE')
DATABASE_URL = os.getenv('DATABASE_URL_TESTE')


@unittest.skipUnless(REDIS_URL and DATABASE_URL, 'defina REDIS_URL_TESTE e DATABASE_URL_TESTE')
class SincronizadorPostgresTest(unittest.TestCase):
    def setUp(self):
        import psycopg2
        import redis

        self.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.redis.flushdb()
        self.pg = psycopg2.connect(DATABASE_URL)
        with self.pg, self.pg.cursor() as cur:
            cur.execute('DROP TABLE IF EXISTS rota_execucoes_paradas, rota_execucoes, funcionarios')
            # só o que a FK de rota_execucoes precisa do schema da Spring
            cur.execute('CREATE TABLE funcionarios (funcionario_id UUID PRIMARY KEY)')
            self.motorista = str(uuid.uuid4())
            cur.execute('INSERT INTO funcionarios VALUES (%s)', (self.motorista,))

        # o sincronizador lê REDIS_URL ao conectar: o ambiente fica valendo durante o teste
        ambiente = patch.dict(os.environ, {'REDIS_URL': REDIS_URL, 'DATABASE_URL': DATABASE_URL})
        ambiente.start()
        self.addCleanup(ambiente.stop)
        with patch('dotenv.load_dotenv'):
            self.sinc = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'sincronizador.py'))
            with patch('threading.Thread'):
                self.api = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))
        self.api['criar_rota'].__globals__['executar_sincronizacao'] = MagicMock()
        self.client = self.api['app'].test_client()
        self.classe = self.sinc['SincronizadorRotasPostgreSQL']
        self.classe._conectar_postgres.__globals__['POSTGRES_URL'] = DATABASE_URL

    def tearDown(self):
        self.pg.close()
        self.redis.close()

    def sincronizador(self):
        s = self.classe()
        s._criar_tabelas()
        return s

    def test_viagem_finalizada_vai_para_o_postgres_e_sai_do_redis(self):
        rota = str(uuid.uuid4())
        enderecos = [str(uuid.uuid4()) for _ in range(12)]
        self.assertEqual(self.client.post('/rotas', json={'rota_id': rota, 'motorista_id': self.motorista}).status_code, 201)
        paradas = [{'endereco': f'Rua {i}', 'endereco_id': e, 'ordem': i + 1} for i, e in enumerate(enderecos)]
        paradas[5]['endereco_id'] = 'nao-e-uuid'  # não pode derrubar a gravação
        self.client.post(f'/rotas/{rota}/paradas', json={'paradas': paradas})
        for i in (0, 1, 10):
            self.client.put(f'/rotas/{rota}/parada/{i}', json={'status': 'CONCLUIDA'})
        self.assertEqual(self.client.put(f'/rotas/{rota}/finalizar').status_code, 200)

        s = self.sincronizador()
        try:
            self.assertTrue(s.sincronizar_rota_finalizada(rota))
            # um segundo ciclo não grava de novo
            self.assertEqual(s.processar_todas_rotas_finalizadas(), 0)
        finally:
            s.fechar()

        with self.pg.cursor() as cur:
            cur.execute('SELECT motorista_id::text, status_final, total_paradas, paradas_concluidas '
                        'FROM rota_execucoes WHERE rota_id = %s', (rota,))
            self.assertEqual(cur.fetchall(), [(self.motorista, 'FINALIZADA', 12, 3)])
            cur.execute('SELECT ordem, endereco_id::text, status, data_conclusao IS NOT NULL '
                        'FROM rota_execucoes_paradas ORDER BY ordem')
            linhas = cur.fetchall()
        self.assertEqual([l[0] for l in linhas], list(range(12)))
        self.assertEqual(linhas[10], (10, enderecos[10], 'CONCLUIDA', True))
        self.assertEqual(linhas[2], (2, enderecos[2], 'PENDENTE', False))
        self.assertIsNone(linhas[5][1])
        self.assertEqual(self.redis.keys(f'rota:*{rota}*'), [])

    def test_criar_tabelas_acrescenta_colunas_em_tabela_antiga(self):
        with self.pg, self.pg.cursor() as cur:
            cur.execute("""CREATE TABLE rota_execucoes (execucao_id UUID PRIMARY KEY, rota_id UUID NOT NULL,
                           motorista_id UUID NOT NULL, status_final VARCHAR(50), total_paradas INTEGER,
                           paradas_concluidas INTEGER, peso_estimado NUMERIC(10,3), peso_coletado NUMERIC(10,3),
                           tempo_total_minutos INTEGER, data_inicio TIMESTAMP, data_finalizacao TIMESTAMP,
                           data_sincronizacao TIMESTAMP DEFAULT NOW())""")
            cur.execute("""CREATE TABLE rota_execucoes_paradas (parada_execucao_id UUID PRIMARY KEY,
                           execucao_id UUID NOT NULL, endereco VARCHAR(255), material VARCHAR(100),
                           peso_estimado NUMERIC(10,3), peso_coletado NUMERIC(10,3), status VARCHAR(50),
                           observacoes TEXT)""")
        self.sincronizador().fechar()
        with self.pg.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'rota_execucoes_paradas'")
            colunas = {c for (c,) in cur.fetchall()}
        self.assertTrue({'ordem', 'endereco_id', 'data_conclusao', 'observacoes'} <= colunas)


if __name__ == '__main__':
    unittest.main()
