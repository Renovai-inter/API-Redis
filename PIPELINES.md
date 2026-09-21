# Pipelines CI/CD - API Redis

Três pipelines simples e diretos usando GitHub Actions.

## 📋 Pipelines

### 1. **Build** (.github/workflows/build.yml)
- Verifica se o código compila
- Instala dependências
- Testa se a aplicação carrega

### 2. **QA** (.github/workflows/qa.yml)
- Roda os testes com pytest
- Requer Redis em localhost:6379

### 3. **PROD** (.github/workflows/prod.yml)
- Deploy para produção (na branch main)
- Placeholder para customização

## 🧪 Rodar testes localmente

```bash
# Instalar dependências
pip install -r requirements.txt

# Rodar testes
pytest
```

## 📦 Estrutura

```
.github/workflows/
├── build.yml    # Build pipeline
├── qa.yml       # Testes
└── prod.yml     # Deploy

tests/
└── test_main.py # Testes simples (4 testes)

requirements.txt # Dependências (essenciais + pytest)
```

## ✅ Testes Inclusos

- `test_health()` - Testa endpoint /health
- `test_criar_rota_sem_parametros()` - Testa validação
- `test_criar_rota_com_parametros()` - Testa criação
- `test_app_loads()` - Testa se app carrega
