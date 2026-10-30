"""Contrato da viagem do motorista (o que o app Mobile usa), com um Redis em memória (fakeredis)."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import MagicMock, patch

import fakeredis

ROTA = 'a1b2c3d4-0000-4000-8000-000000000001'
MOTORISTA = 'f0f0f0f0-0000-4000-8000-000000000001'
OUTRO = 'f0f0f0f0-0000-4000-8000-000000000002'


def carregar_api(redis_fake):
    with patch('redis_connection.conectar_redis', return_value=redis_fake), \
         patch('dotenv.load_dotenv'), patch('threading.Thread'), \
         patch.dict('sys.modules', sincronizador=MagicMock()):
        modulo = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))
    # a finalização dispara a gravação no Postgres numa thread: aqui só registra a chamada
    modulo['criar_rota'].__globals__['executar_sincronizacao'] = MagicMock()
    return modulo


class ViagemTest(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True)
        self.modulo = carregar_api(self.redis)
        self.app = self.modulo['app']
        self.client = self.app.test_client()

    def iniciar(self, motorista=MOTORISTA):
        return self.client.post('/rotas', json={'rota_id': ROTA, 'motorista_id': motorista})

    def registrar(self, n):
        paradas = [{'endereco': f'Rua {i}', 'endereco_id': f'00000000-0000-4000-8000-{i:012d}',
                    'ordem': i + 1} for i in range(n)]
        return self.client.post(f'/rotas/{ROTA}/paradas', json={'paradas': paradas})

    def test_antes_de_iniciar_responde_404(self):
        self.assertEqual(self.client.get(f'/rotas/{ROTA}').status_code, 404)

    def test_iniciar_e_registrar_paradas(self):
        self.assertEqual(self.iniciar().status_code, 201)
        sessao = self.client.get(f'/rotas/{ROTA}').get_json()
        self.assertEqual(sessao['status'], 'INICIADA')
        self.assertEqual(sessao['motorista_id'], MOTORISTA)
        self.assertEqual(self.registrar(3).status_code, 201)
        paradas = self.client.get(f'/rotas/{ROTA}/paradas').get_json()['paradas']
        self.assertEqual([p['id'] for p in paradas], ['0', '1', '2'])
        self.assertEqual(paradas[1]['endereco_id'], '00000000-0000-4000-8000-000000000001')
        self.assertEqual(paradas[1]['ordem'], '2')
        self.assertTrue(all(p['status'] == 'PENDENTE' for p in paradas))

    def test_mais_de_dez_paradas_voltam_em_ordem_numerica(self):
        self.iniciar()
        self.registrar(12)
        ids = [p['id'] for p in self.client.get(f'/rotas/{ROTA}/paradas').get_json()['paradas']]
        self.assertEqual(ids, [str(i) for i in range(12)])
        # controle: a ordem de texto das chaves é diferente (10 e 11 antes de 2)
        texto = [k.rsplit(':', 1)[1] for k in sorted(self.redis.keys(f'rota:parada:{ROTA}:*'))]
        self.assertNotEqual(texto, ids)

    def test_outro_motorista_nao_assume_a_viagem(self):
        self.iniciar()
        self.registrar(2)
        self.client.put(f'/rotas/{ROTA}/parada/0', json={'status': 'CONCLUIDA'})
        resp = self.iniciar(OUTRO)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['motorista_id'], MOTORISTA)
        sessao = self.client.get(f'/rotas/{ROTA}').get_json()
        self.assertEqual(sessao['motorista_id'], MOTORISTA)
        parada = self.client.get(f'/rotas/{ROTA}/paradas').get_json()['paradas'][0]
        self.assertEqual(parada['status'], 'CONCLUIDA')  # progresso preservado
        put = self.client.put(f'/rotas/{ROTA}/parada/1',
                              json={'status': 'CONCLUIDA', 'motorista_id': OUTRO})
        self.assertEqual(put.status_code, 403)
        fim = self.client.put(f'/rotas/{ROTA}/finalizar', json={'motorista_id': OUTRO})
        self.assertEqual(fim.status_code, 403)

    def test_mesmo_motorista_retoma_sem_perder_progresso(self):
        self.iniciar()
        inicio = self.client.get(f'/rotas/{ROTA}').get_json()['timestamp_inicio']
        self.registrar(2)
        self.client.put(f'/rotas/{ROTA}/parada/0', json={'status': 'CONCLUIDA'})
        self.assertEqual(self.iniciar().status_code, 200)
        self.assertEqual(self.registrar(2).status_code, 200)  # reenvio não zera
        self.assertEqual(self.client.get(f'/rotas/{ROTA}').get_json()['timestamp_inicio'], inicio)
        paradas = self.client.get(f'/rotas/{ROTA}/paradas').get_json()['paradas']
        self.assertEqual([p['status'] for p in paradas], ['CONCLUIDA', 'PENDENTE'])
        self.assertEqual(self.redis.llen(f'rota:fila:{ROTA}'), 2)

    def test_paradas_exigem_viagem_iniciada(self):
        self.assertEqual(self.registrar(2).status_code, 404)

    def test_marcar_e_desmarcar_registra_hora_da_conclusao(self):
        self.iniciar()
        self.registrar(2)
        feita = self.client.put(f'/rotas/{ROTA}/parada/0',
                                json={'status': 'CONCLUIDA', 'motorista_id': MOTORISTA})
        self.assertEqual(feita.status_code, 200)
        self.assertIn('timestamp_conclusao', feita.get_json())
        self.assertEqual(self.client.get(f'/rotas/{ROTA}').get_json()['parada_atual'], '1')
        desfeita = self.client.put(f'/rotas/{ROTA}/parada/0', json={'status': 'PENDENTE'})
        self.assertEqual(desfeita.get_json()['status'], 'PENDENTE')
        self.assertNotIn('timestamp_conclusao', desfeita.get_json())
        progresso = self.client.get(f'/rotas/{ROTA}/progresso').get_json()
        self.assertEqual((progresso['concluidas'], progresso['pendentes']), (0, 2))

    def test_status_invalido_e_corpo_vazio(self):
        self.iniciar()
        self.registrar(1)
        self.assertEqual(self.client.put(f'/rotas/{ROTA}/parada/0', json={'status': 'FEITA'}).status_code, 400)
        self.assertEqual(self.client.put(f'/rotas/{ROTA}/parada/9', json={'status': 'CONCLUIDA'}).status_code, 404)
        self.assertEqual(self.client.post('/rotas', data='x', content_type='text/plain').status_code, 400)

    def test_finalizar_e_idempotente_e_bloqueia_alteracoes(self):
        self.iniciar()
        self.registrar(1)
        primeira = self.client.put(f'/rotas/{ROTA}/finalizar', json={'motorista_id': MOTORISTA})
        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(primeira.get_json()['status'], 'FINALIZADA')
        segunda = self.client.put(f'/rotas/{ROTA}/finalizar')
        self.assertEqual(segunda.get_json()['timestamp_finalizacao'],
                         primeira.get_json()['timestamp_finalizacao'])
        self.assertEqual(self.client.put(f'/rotas/{ROTA}/parada/0', json={'status': 'CONCLUIDA'}).status_code, 409)
        self.assertEqual(self.iniciar().status_code, 409)  # aguardando gravar no Postgres
        self.modulo['criar_rota'].__globals__['executar_sincronizacao'].assert_called_with(ROTA)


if __name__ == '__main__':
    unittest.main()
