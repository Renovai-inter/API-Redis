from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import redis
import json
import threading
import time
from datetime import datetime, timedelta
import os
import uuid

load_dotenv()

from sincronizador import SincronizadorRotasPostgreSQL
from redis_connection import conectar_redis

app = Flask(__name__)
CORS(app)

intervalo_sincronizacao = int(os.getenv('INTERVALO_SINCRONIZACAO', 300))

r = conectar_redis()

lock_sincronizacao = threading.Lock()

def executar_sincronizacao(rota_id=None):
    with lock_sincronizacao:
        sincronizador = None
        try:
            sincronizador = SincronizadorRotasPostgreSQL()
            if rota_id:
                sincronizador.sincronizar_rota_finalizada(rota_id)
            else:
                sincronizador.processar_todas_rotas_finalizadas()
            sincronizador.sincronizar_dau_postgres()
        except Exception as e:
            print(f"Erro na sincronizacao: {e}")
        finally:
            if sincronizador:
                sincronizador.fechar()

STATUS_PARADA = ('PENDENTE', 'EM_ANDAMENTO', 'CONCLUIDA')


def _corpo():
    """Corpo JSON da requisição; sem corpo (ou inválido) vira {} em vez de erro 500."""
    return request.get_json(silent=True) or {}


def _eh_uuid_valido(val):
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, TypeError, AttributeError):
        return False

def _indice_parada(chave):
    try:
        return int(chave.rsplit(':', 1)[1])
    except (IndexError, ValueError):
        return 1 << 30


def chaves_paradas(cliente, rota_id):
    """Chaves das paradas na ordem numérica (0, 1, 2, …, 10): a ordem de texto poria 10 antes de 2."""
    return sorted(cliente.keys(f"rota:parada:{rota_id}:*"), key=_indice_parada)


def _motorista_diferente(sessao, motorista_id):
    return bool(motorista_id) and str(sessao.get('motorista_id', '')).lower() != str(motorista_id).lower()


def loop_sincronizacao():
    try:
        sincronizador = SincronizadorRotasPostgreSQL()
        sincronizador._criar_tabelas()
        sincronizador.fechar()
    except Exception as e:
        print(f"Erro ao criar tabelas: {e}")

    while True:
        executar_sincronizacao()
        time.sleep(intervalo_sincronizacao)

@app.route('/health', methods=['GET'])
def health():
    if r:
        try:
            r.ping()
            return jsonify({'status': 'ok', 'redis': 'connected'}), 200
        except redis.RedisError:
            return jsonify({'status': 'error', 'redis': 'disconnected'}), 503
    return jsonify({'status': 'error', 'redis': 'disconnected'}), 503

@app.route('/rotas', methods=['POST'])
def criar_rota():
    data = _corpo()
    rota_id = data.get('rota_id')
    motorista_id = data.get('motorista_id')

    if not rota_id or not motorista_id:
        return jsonify({'erro': 'rota_id e motorista_id obrigatorios'}), 400

    if r is None:
        return jsonify({'erro': 'redis indisponivel'}), 500

    sessao_key = f"rota:sessao:{rota_id}"

    # Uma viagem por rota: quem iniciou continua dono dela até finalizar. Sem isso, um segundo
    # POST apagaria o andamento (e o motorista) da viagem em curso.
    existente = r.hgetall(sessao_key)
    if existente:
        if existente.get('status') == 'FINALIZADA':
            threading.Thread(target=executar_sincronizacao, args=(rota_id,), daemon=True).start()
            return jsonify({'erro': 'Rota finalizada aguardando sincronizacao',
                            'status': 'FINALIZADA'}), 409
        if _motorista_diferente(existente, motorista_id):
            return jsonify({'erro': 'Rota em andamento com outro motorista',
                            'motorista_id': existente.get('motorista_id'),
                            'status': existente.get('status')}), 409
        return jsonify({'mensagem': 'Rota ja iniciada', 'rota_id': rota_id}), 200

    sessao_data = {
        'rota_id': rota_id,
        'motorista_id': motorista_id,
        'status': 'INICIADA',
        'parada_atual': 0,
        'timestamp_inicio': datetime.now().isoformat(),
        'timestamp_atualizacao': datetime.now().isoformat()
    }

    r.hset(sessao_key, mapping=sessao_data)
    r.expire(sessao_key, 43200)

    return jsonify({'mensagem': 'Rota criada', 'rota_id': rota_id}), 201

@app.route('/rotas/<rota_id>', methods=['GET'])
def obter_rota(rota_id):
    sessao_key = f"rota:sessao:{rota_id}"
    
    if not r.exists(sessao_key):
        return jsonify({'erro': 'Rota nao encontrada'}), 404
    
    sessao = r.hgetall(sessao_key)
    return jsonify(sessao), 200

@app.route('/rotas/<rota_id>/paradas', methods=['POST'])
def adicionar_paradas(rota_id):
    data = _corpo()
    paradas = data.get('paradas', [])

    if not paradas:
        return jsonify({'erro': 'Lista de paradas vazia'}), 400

    if not r.exists(f"rota:sessao:{rota_id}"):
        return jsonify({'erro': 'Rota nao iniciada'}), 404

    # Reenvio (ex.: o app não recebeu a primeira resposta): mantém as paradas e o status já
    # registrados em vez de zerar o progresso e duplicar a fila.
    if chaves_paradas(r, rota_id):
        return jsonify({'mensagem': 'Paradas ja registradas'}), 200

    for idx, parada in enumerate(paradas):
        parada_key = f"rota:parada:{rota_id}:{idx}"
        parada_data = {
            'id': idx,
            'rota_id': rota_id,
            'ordem': parada.get('ordem', idx),
            'endereco_id': parada.get('endereco_id') or '',
            'endereco': parada.get('endereco', ''),
            'material': parada.get('material', ''),
            'peso_estimado': parada.get('peso_estimado', 0),
            'status': 'PENDENTE',
            'timestamp_criacao': datetime.now().isoformat()
        }
        r.hset(parada_key, mapping=parada_data)
        r.expire(parada_key, 43200)

        fila_key = f"rota:fila:{rota_id}"
        r.rpush(fila_key, json.dumps({'parada_id': idx, 'endereco': parada.get('endereco')}))
        r.expire(fila_key, 86400)

    return jsonify({'mensagem': f'{len(paradas)} paradas adicionadas'}), 201

@app.route('/rotas/<rota_id>/paradas', methods=['GET'])
def listar_paradas(rota_id):
    parada_keys = chaves_paradas(r, rota_id)

    if not parada_keys:
        return jsonify({'paradas': []}), 200

    paradas = []
    for key in parada_keys:
        parada = r.hgetall(key)
        paradas.append(parada)

    return jsonify({'paradas': paradas}), 200

@app.route('/rotas/<rota_id>/parada/<int:parada_id>', methods=['PUT'])
def atualizar_parada(rota_id, parada_id):
    data = _corpo()
    novo_status = data.get('status')

    parada_key = f"rota:parada:{rota_id}:{parada_id}"

    if not r.exists(parada_key):
        return jsonify({'erro': 'Parada nao encontrada'}), 404

    if novo_status and novo_status not in STATUS_PARADA:
        return jsonify({'erro': f'status deve ser um de {", ".join(STATUS_PARADA)}'}), 400

    sessao_key = f"rota:sessao:{rota_id}"
    sessao = r.hgetall(sessao_key)
    if sessao.get('status') == 'FINALIZADA':
        return jsonify({'erro': 'Rota ja finalizada'}), 409
    if _motorista_diferente(sessao, data.get('motorista_id')):
        return jsonify({'erro': 'Rota em andamento com outro motorista'}), 403

    agora = datetime.now().isoformat()
    if novo_status:
        r.hset(parada_key, 'status', novo_status)
        r.hset(parada_key, 'timestamp_atualizacao', agora)
        # Hora em que a coleta foi concluída: dá o tempo real de cada trecho para o modelo de rotas.
        if novo_status == 'CONCLUIDA':
            r.hset(parada_key, 'timestamp_conclusao', agora)
        else:
            r.hdel(parada_key, 'timestamp_conclusao')
        if sessao:
            pendente = next((_indice_parada(k) for k in chaves_paradas(r, rota_id)
                             if r.hget(k, 'status') != 'CONCLUIDA'), None)
            r.hset(sessao_key, mapping={
                'timestamp_atualizacao': agora,
                'parada_atual': pendente if pendente is not None else -1,
            })

    if data.get('peso_real'):
        r.hset(parada_key, 'peso_real', data.get('peso_real'))

    if data.get('observacoes'):
        r.hset(parada_key, 'observacoes', data.get('observacoes'))

    parada_atualizada = r.hgetall(parada_key)
    return jsonify(parada_atualizada), 200

@app.route('/rotas/<rota_id>/progresso', methods=['GET'])
def obter_progresso(rota_id):
    parada_keys = r.keys(f"rota:parada:{rota_id}:*")
    
    if not parada_keys:
        return jsonify({'progresso': 0, 'paradas_totais': 0}), 200
    
    paradas = [r.hgetall(key) for key in parada_keys]
    
    concluidas = sum(1 for p in paradas if p.get('status') == 'CONCLUIDA')
    pendentes = sum(1 for p in paradas if p.get('status') == 'PENDENTE')
    em_andamento = sum(1 for p in paradas if p.get('status') == 'EM_ANDAMENTO')
    
    total = len(paradas)
    progresso = (concluidas / total * 100) if total > 0 else 0
    
    return jsonify({
        'progresso': round(progresso, 2),
        'paradas_totais': total,
        'concluidas': concluidas,
        'pendentes': pendentes,
        'em_andamento': em_andamento
    }), 200

@app.route('/rotas/<rota_id>/finalizar', methods=['PUT'])
def finalizar_rota(rota_id):
    sessao_key = f"rota:sessao:{rota_id}"

    if not r.exists(sessao_key):
        return jsonify({'erro': 'Rota nao encontrada'}), 404

    sessao = r.hgetall(sessao_key)
    if _motorista_diferente(sessao, _corpo().get('motorista_id')):
        return jsonify({'erro': 'Rota em andamento com outro motorista'}), 403

    # Repetir o pedido (resposta perdida no caminho) não muda a hora de finalização.
    if sessao.get('status') != 'FINALIZADA':
        r.hset(sessao_key, 'status', 'FINALIZADA')
        r.hset(sessao_key, 'timestamp_finalizacao', datetime.now().isoformat())

    sessao_atualizada = r.hgetall(sessao_key)
    threading.Thread(target=executar_sincronizacao, args=(rota_id,), daemon=True).start()
    return jsonify(sessao_atualizada), 200

@app.route('/rotas/<rota_id>/fila', methods=['GET'])
def obter_fila(rota_id):
    fila_key = f"rota:fila:{rota_id}"
    
    fila = r.lrange(fila_key, 0, -1)
    fila_parsed = [json.loads(item) for item in fila]
    
    return jsonify({'fila': fila_parsed}), 200

@app.route('/rotas/<rota_id>/fila', methods=['POST'])
def adicionar_fila(rota_id):
    data = _corpo()
    item = data.get('item')
    
    if not item:
        return jsonify({'erro': 'Item obrigatorio'}), 400
    
    fila_key = f"rota:fila:{rota_id}"
    r.rpush(fila_key, json.dumps(item))
    r.expire(fila_key, 86400)
    
    return jsonify({'mensagem': 'Item adicionado a fila'}), 201

@app.route('/rotas/<rota_id>/fila/proxima', methods=['GET'])
def proxima_fila(rota_id):
    fila_key = f"rota:fila:{rota_id}"
    
    item = r.lpop(fila_key)
    
    if not item:
        return jsonify({'erro': 'Fila vazia'}), 404
    
    item_parsed = json.loads(item)
    return jsonify(item_parsed), 200

@app.route('/rotas/<rota_id>/stats', methods=['GET'])
def obter_stats(rota_id):
    sessao_key = f"rota:sessao:{rota_id}"
    parada_keys = r.keys(f"rota:parada:{rota_id}:*")
    
    if not r.exists(sessao_key):
        return jsonify({'erro': 'Rota nao encontrada'}), 404
    
    sessao = r.hgetall(sessao_key)
    paradas = [r.hgetall(key) for key in parada_keys]
    
    peso_total_estimado = sum(float(p.get('peso_estimado', 0)) for p in paradas)
    peso_total_real = sum(float(p.get('peso_real', 0)) for p in paradas if p.get('peso_real'))
    
    return jsonify({
        'rota_id': rota_id,
        'motorista_id': sessao.get('motorista_id'),
        'status': sessao.get('status'),
        'total_paradas': len(paradas),
        'peso_estimado': peso_total_estimado,
        'peso_coletado': peso_total_real,
        'timestamp_inicio': sessao.get('timestamp_inicio'),
        'timestamp_atualizacao': sessao.get('timestamp_atualizacao')
    }), 200

# ============================================================================
# ENDPOINTS DE ATIVIDADE DE USUÁRIOS E DAU (BITMAP + SYNC POSTGRESQL)
# ============================================================================

@app.route('/atividade/login', methods=['POST'])
def registrar_atividade():
    """Registra atividade de login do usuario (cooperado ou empresa).
    Marca o bit correspondente ao dia do mes no Bitmap Redis e enfileira
    para sincronizacao em lote no PostgreSQL (DAU).
    """
    if r is None:
        return jsonify({'erro': 'redis indisponivel'}), 503

    data = _corpo()
    tipo = str(data.get('tipo', '')).strip().lower()
    entidade_id = str(data.get('id', '')).strip()

    if tipo not in ('cooperado', 'empresa') or not _eh_uuid_valido(entidade_id):
        return jsonify({
            'erro': "Parametros invalidos: 'tipo' deve ser 'cooperado' ou 'empresa', e 'id' deve ser um UUID valido"
        }), 400

    hoje = datetime.now()
    ano_mes = hoje.strftime('%Y-%m')
    dia_idx = hoje.day - 1  # 0 a 30 (offset do bit)
    data_iso = hoje.strftime('%Y-%m-%d')

    chave_bitmap = f"atividade:{tipo}:{entidade_id}:{ano_mes}"
    chave_pendentes = f"dau:pendentes:{data_iso}"

    try:
        pipe = r.pipeline()
        pipe.setbit(chave_bitmap, dia_idx, 1)
        pipe.expire(chave_bitmap, 90 * 86400)  # Mantém 90 dias de histórico

        pipe.sadd(chave_pendentes, f"{tipo}:{entidade_id}")
        pipe.expire(chave_pendentes, 3 * 86400)
        pipe.execute()

        return jsonify({
            'status': 'ok',
            'tipo': tipo,
            'id': entidade_id,
            'dia': hoje.day,
            'mes': ano_mes,
            'ativo': True
        }), 200
    except redis.RedisError as e:
        return jsonify({'erro': f'Erro ao registrar atividade no Redis: {str(e)}'}), 500

@app.route('/atividade/<tipo>/<entidade_id>', methods=['GET'])
def obter_atividade(tipo, entidade_id):
    """Retorna métricas de atividade de um usuário no mês (baseado no Bitmap do Redis)."""
    if r is None:
        return jsonify({'erro': 'redis indisponivel'}), 503

    tipo = str(tipo).strip().lower()
    if tipo not in ('cooperado', 'empresa') or not _eh_uuid_valido(entidade_id):
        return jsonify({'erro': "Parametros invalidos: tipo deve ser 'cooperado' ou 'empresa' e id deve ser um UUID valido"}), 400

    hoje = datetime.now()
    ano_mes = request.args.get('mes') or hoje.strftime('%Y-%m')
    dia_idx = hoje.day - 1

    chave_bitmap = f"atividade:{tipo}:{entidade_id}:{ano_mes}"

    try:
        if not r.exists(chave_bitmap):
            return jsonify({
                'tipo': tipo,
                'id': entidade_id,
                'mes': ano_mes,
                'dias_ativos_mes': 0,
                'ativo_hoje': False
            }), 200

        dias_ativos = r.bitcount(chave_bitmap)
        ativo_hoje = bool(r.getbit(chave_bitmap, dia_idx))

        return jsonify({
            'tipo': tipo,
            'id': entidade_id,
            'mes': ano_mes,
            'dias_ativos_mes': dias_ativos,
            'ativo_hoje': ativo_hoje
        }), 200
    except redis.RedisError as e:
        return jsonify({'erro': str(e)}), 500

@app.route('/atividade/sincronizar', methods=['POST'])
def sincronizar_dau_manual():
    """Dispara sincronização manual de DAU para o PostgreSQL (útil para testes e crons)."""
    try:
        sincronizador = SincronizadorRotasPostgreSQL()
        total = sincronizador.sincronizar_dau_postgres()
        sincronizador.fechar()
        return jsonify({'status': 'ok', 'total_sincronizados': total}), 200
    except Exception as e:
        return jsonify({'erro': f'Falha na sincronizacao: {str(e)}'}), 500

@app.errorhandler(404)
def nao_encontrado(error):
    return jsonify({'erro': 'Rota nao encontrada'}), 404

@app.errorhandler(500)
def erro_interno(error):
    return jsonify({'erro': 'Erro interno do servidor'}), 500

threading.Thread(target=loop_sincronizacao, daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
