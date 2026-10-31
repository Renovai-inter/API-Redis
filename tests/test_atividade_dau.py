"""Testes dos endpoints de atividade de usuários e sincronização de DAU (Redis -> PostgreSQL)."""
from datetime import datetime
from pathlib import Path
import runpy
import unittest
from unittest.mock import MagicMock, patch
import uuid

import fakeredis

COOPERADO_ID = str(uuid.uuid4())
EMPRESA_ID = str(uuid.uuid4())


def carregar_api(redis_fake):
    with patch('redis_connection.conectar_redis', return_value=redis_fake), \
         patch('dotenv.load_dotenv'), patch('threading.Thread'), \
         patch.dict('sys.modules', sincronizador=MagicMock()):
        modulo = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))
    return modulo


class AtividadeDAUTest(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=False)
        self.modulo = carregar_api(self.redis)
        self.app = self.modulo['app']
        self.client = self.app.test_client()

    def test_validacao_parametros_invalidos(self):
        """Testa validação de campos obrigatórios e formato de UUID"""
        # Sem corpo
        res = self.client.post('/atividade/login', json={})
        self.assertEqual(res.status_code, 400)

        # Tipo inválido
        res = self.client.post('/atividade/login', json={'tipo': 'admin', 'id': COOPERADO_ID})
        self.assertEqual(res.status_code, 400)

        # UUID inválido
        res = self.client.post('/atividade/login', json={'tipo': 'cooperado', 'id': 'invalido-123'})
        self.assertEqual(res.status_code, 400)

    def test_registrar_atividade_cooperado_sucesso(self):
        """Testa registro de login de cooperado gerando bitmap e entrada no set de pendentes"""
        hoje = datetime.now()
        dia_idx = hoje.day - 1
        ano_mes = hoje.strftime('%Y-%m')
        data_iso = hoje.strftime('%Y-%m-%d')

        res = self.client.post('/atividade/login', json={'tipo': 'cooperado', 'id': COOPERADO_ID})
        self.assertEqual(res.status_code, 200)
        dados = res.get_json()
        self.assertEqual(dados['status'], 'ok')
        self.assertEqual(dados['tipo'], 'cooperado')
        self.assertEqual(dados['id'], COOPERADO_ID)
        self.assertEqual(dados['dia'], hoje.day)

        # Verifica se o bit foi marcado no Redis
        chave_bitmap = f"atividade:cooperado:{COOPERADO_ID}:{ano_mes}".encode('utf-8')
        bit_val = self.redis.getbit(chave_bitmap, dia_idx)
        self.assertEqual(bit_val, 1)

        # Verifica se foi enfileirado para o sincronizador
        chave_pendentes = f"dau:pendentes:{data_iso}".encode('utf-8')
        membros = self.redis.smembers(chave_pendentes)
        self.assertIn(f"cooperado:{COOPERADO_ID}".encode('utf-8'), membros)

    def test_obter_estatisticas_atividade_usuario(self):
        """Testa consulta GET de atividade no mês via bitcount e getbit"""
        hoje = datetime.now()
        ano_mes = hoje.strftime('%Y-%m')

        # Antes do login: 0 dias
        res = self.client.get(f'/atividade/cooperado/{COOPERADO_ID}')
        self.assertEqual(res.status_code, 200)
        dados = res.get_json()
        self.assertEqual(dados['dias_ativos_mes'], 0)
        self.assertFalse(dados['ativo_hoje'])

        # Faz login
        self.client.post('/atividade/login', json={'tipo': 'cooperado', 'id': COOPERADO_ID})

        # Após o login: 1 dia ativo e ativo_hoje = True
        res = self.client.get(f'/atividade/cooperado/{COOPERADO_ID}')
        self.assertEqual(res.status_code, 200)
        dados = res.get_json()
        self.assertEqual(dados['dias_ativos_mes'], 1)
        self.assertTrue(dados['ativo_hoje'])

    def test_sincronizador_dau_postgres(self):
        """Testa lógica do método sincronizar_dau_postgres usando FakeRedis e Mock do Postgres"""
        from sincronizador import SincronizadorRotasPostgreSQL

        hoje_str = datetime.now().strftime('%Y-%m-%d')
        chave_pendente = f"dau:pendentes:{hoje_str}"

        # Popula o fake redis com acessos pendentes
        self.redis.sadd(chave_pendente, f"cooperado:{COOPERADO_ID}")
        self.redis.sadd(chave_pendente, f"empresa:{EMPRESA_ID}")

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.encoding = 'utf-8'
        mock_cursor.connection = mock_conn

        with patch.object(SincronizadorRotasPostgreSQL, '_conectar_redis'), \
             patch.object(SincronizadorRotasPostgreSQL, '_conectar_postgres'), \
             patch('sincronizador.execute_values'):
            sinc = SincronizadorRotasPostgreSQL()
            sinc.redis_client = self.redis
            sinc.postgres_conn = mock_conn

            total = sinc.sincronizar_dau_postgres()
            self.assertEqual(total, 2)
            self.assertTrue(mock_conn.commit.called)

            # Verifica se o set de pendentes foi limpo após o commit
            self.assertFalse(self.redis.exists(chave_pendente))
