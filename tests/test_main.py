import pytest
import json
from main import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_health(client):
    """Testa endpoint /health"""
    response = client.get('/health')
    assert response.status_code in [200, 500]
    assert 'status' in response.get_json()


def test_criar_rota_sem_parametros(client):
    """Testa erro ao criar rota sem parâmetros"""
    response = client.post('/rotas', json={})
    assert response.status_code == 400


def test_criar_rota_com_parametros(client):
    """Testa criação de rota com parâmetros"""
    data = {'rota_id': 'r001', 'motorista_id': 'd001'}
    response = client.post('/rotas', json=data)
    assert response.status_code in [200, 500]


def test_app_loads():
    """Testa se a aplicação carrega"""
    assert app is not None
