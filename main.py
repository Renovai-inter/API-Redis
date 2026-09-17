from flask import Flask, request, jsonify
from flask_cors import CORS
import redis
import json
from datetime import datetime, timedelta
import os

app = Flask(__name__)
CORS(app)

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_port = int(os.getenv('REDIS_PORT', 6379))
redis_password = os.getenv('REDIS_PASSWORD', '')

try:
    r = redis.Redis(host=redis_host, port=redis_port, password=redis_password, decode_responses=True)
    r.ping()
except Exception as e:
    print(f"Erro ao conectar Redis: {e}")
    r = None

@app.route('/health', methods=['GET'])
def health():
    if r:
        try:
            r.ping()
            return jsonify({'status': 'ok', 'redis': 'connected'}), 200
        except:
            return jsonify({'status': 'ok', 'redis': 'disconnected'}), 200
    return jsonify({'status': 'error'}), 500

@app.route('/rotas', methods=['POST'])
def criar_rota():
    data = request.get_json()
    rota_id = data.get('rota_id')
    motorista_id = data.get('motorista_id')
    
    if not rota_id or not motorista_id:
        return jsonify({'erro': 'rota_id e motorista_id obrigatorios'}), 400
    
    sessao_key = f"rota:sessao:{rota_id}"
    
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
    data = request.get_json()
    paradas = data.get('paradas', [])
    
    if not paradas:
        return jsonify({'erro': 'Lista de paradas vazia'}), 400
    
    for idx, parada in enumerate(paradas):
        parada_key = f"rota:parada:{rota_id}:{idx}"
        parada_data = {
            'id': idx,
            'rota_id': rota_id,
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
    parada_keys = r.keys(f"rota:parada:{rota_id}:*")
    
    if not parada_keys:
        return jsonify({'paradas': []}), 200
    
    paradas = []
    for key in sorted(parada_keys):
        parada = r.hgetall(key)
        paradas.append(parada)
    
    return jsonify({'paradas': paradas}), 200

@app.route('/rotas/<rota_id>/parada/<int:parada_id>', methods=['PUT'])
def atualizar_parada(rota_id, parada_id):
    data = request.get_json()
    novo_status = data.get('status')
    
    parada_key = f"rota:parada:{rota_id}:{parada_id}"
    
    if not r.exists(parada_key):
        return jsonify({'erro': 'Parada nao encontrada'}), 404
    
    if novo_status:
        r.hset(parada_key, 'status', novo_status)
        r.hset(parada_key, 'timestamp_atualizacao', datetime.now().isoformat())
    
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
    
    r.hset(sessao_key, 'status', 'FINALIZADA')
    r.hset(sessao_key, 'timestamp_finalizacao', datetime.now().isoformat())
    
    sessao_atualizada = r.hgetall(sessao_key)
    return jsonify(sessao_atualizada), 200

@app.route('/rotas/<rota_id>/fila', methods=['GET'])
def obter_fila(rota_id):
    fila_key = f"rota:fila:{rota_id}"
    
    fila = r.lrange(fila_key, 0, -1)
    fila_parsed = [json.loads(item) for item in fila]
    
    return jsonify({'fila': fila_parsed}), 200

@app.route('/rotas/<rota_id>/fila', methods=['POST'])
def adicionar_fila(rota_id):
    data = request.get_json()
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

@app.errorhandler(404)
def nao_encontrado(error):
    return jsonify({'erro': 'Rota nao encontrada'}), 404

@app.errorhandler(500)
def erro_interno(error):
    return jsonify({'erro': 'Erro interno do servidor'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
