#!/usr/bin/env python3
"""
SINCRONIZADOR ROTAS REDIS → POSTGRESQL
Sincroniza dados de rotas finalizadas do Redis para o banco PostgreSQL real
Arquivo: sincronizador-rotas-postgresql.py
Execução: python sincronizador-rotas-postgresql.py &
Agendar via cron: */5 * * * * python /path/sincronizador-rotas-postgresql.py
"""

import redis
import psycopg2
from psycopg2.extras import execute_values
import json
import logging
from datetime import datetime
import os
from typing import Dict, List, Optional
import uuid

import uuid
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

# Redis
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')

# PostgreSQL (Neon - usa URL de conexão)
POSTGRES_URL = os.getenv(
    'DATABASE_URL',
    ''
)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================================
# SINCRONIZADOR
# ============================================================================

class SincronizadorRotasPostgreSQL:
    """Sincroniza dados de rotas do Redis para PostgreSQL"""
    
    def __init__(self):
        """Inicializa conexões com Redis e PostgreSQL"""
        self.redis_client = None
        self.postgres_conn = None
        self._conectar_redis()
        self._conectar_postgres()
    
    def _conectar_redis(self) -> None:
        """Conecta ao Redis"""
        try:
            self.redis_client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                protocol=2,
                decode_responses=True
            )
            self.redis_client.ping()
            logger.info("✓ Conectado ao Redis")
        except Exception as e:
            logger.error(f"✗ Erro ao conectar ao Redis: {e}")
            raise
    
    def _conectar_postgres(self) -> None:
        """Conecta ao PostgreSQL"""
        try:
            self.postgres_conn = psycopg2.connect(POSTGRES_URL)
            logger.info("✓ Conectado ao PostgreSQL")
        except Exception as e:
            logger.error(f"✗ Erro ao conectar ao PostgreSQL: {e}")
            raise
    
    def _criar_tabelas(self) -> None:
        """Cria tabelas de auditoria se não existirem"""
        try:
            cursor = self.postgres_conn.cursor()
            
            # Tabela de execuções de rotas
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rota_execucoes (
                    execucao_id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    rota_id UUID NOT NULL,
                    motorista_id UUID NOT NULL,
                    status_final VARCHAR(50),
                    total_paradas INTEGER,
                    paradas_concluidas INTEGER,
                    peso_estimado NUMERIC(10, 3),
                    peso_coletado NUMERIC(10, 3),
                    tempo_total_minutos INTEGER,
                    data_inicio TIMESTAMP,
                    data_finalizacao TIMESTAMP,
                    data_sincronizacao TIMESTAMP DEFAULT NOW(),
                    
                    FOREIGN KEY (motorista_id) REFERENCES funcionarios(funcionario_id) ON DELETE SET NULL
                );
            """)
            
            # Tabela de paradas executadas
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rota_execucoes_paradas (
                    parada_execucao_id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
                    execucao_id UUID NOT NULL,
                    endereco VARCHAR(255),
                    material VARCHAR(100),
                    peso_estimado NUMERIC(10, 3),
                    peso_coletado NUMERIC(10, 3),
                    status VARCHAR(50),
                    observacoes TEXT,
                    
                    FOREIGN KEY (execucao_id) REFERENCES rota_execucoes(execucao_id) ON DELETE CASCADE
                );
            """)
            
            self.postgres_conn.commit()
            logger.info("✓ Tabelas de auditoria verificadas/criadas")
        except Exception as e:
            self.postgres_conn.rollback()
            logger.error(f"✗ Erro ao criar tabelas: {e}")
            raise
    
    def _obter_rota_redis(self, rota_id: str) -> Optional[Dict]:
        """Obtém dados completos da rota no Redis"""
        try:
            # Obter sessão
            sessao_key = f"rota:sessao:{rota_id}"
            sessao = self.redis_client.hgetall(sessao_key)
            
            if not sessao:
                return None
            
            # Obter todas as paradas
            parada_keys = self.redis_client.keys(f"rota:parada:{rota_id}:*")
            paradas = []
            
            for key in sorted(parada_keys):
                parada = self.redis_client.hgetall(key)
                paradas.append(parada)
            
            return {
                'sessao': sessao,
                'paradas': paradas
            }
        except Exception as e:
            logger.error(f"✗ Erro ao obter rota do Redis: {e}")
            return None
    
    def _calcular_tempo_total(self, timestamp_inicio: str, timestamp_fim: str) -> int:
        try:
            inicio = datetime.fromisoformat(timestamp_inicio)
            fim = datetime.fromisoformat(timestamp_fim)
            return int((fim - inicio).total_seconds() / 60)
        except Exception:
            return 0
    
    def _persistir_rota_postgres(self, rota_id: str, dados: Dict) -> bool:
        """Persiste dados da rota no PostgreSQL"""
        try:
            cursor = self.postgres_conn.cursor()
            
            sessao = dados['sessao']
            paradas = dados['paradas']
            
            # Calcular totalizações
            peso_estimado = sum(float(p.get('peso_estimado', 0)) for p in paradas)
            peso_coletado = sum(float(p.get('peso_real', 0)) for p in paradas if p.get('peso_real'))
            paradas_concluidas = sum(1 for p in paradas if p.get('status') == 'CONCLUIDA')
            tempo_total = self._calcular_tempo_total(
                sessao.get('timestamp_inicio'),
                sessao.get('timestamp_finalizacao')
            )
            
            # Converter timestamps
            data_inicio = datetime.fromisoformat(sessao.get('timestamp_inicio', ''))
            data_fim = datetime.fromisoformat(sessao.get('timestamp_finalizacao', ''))
            
            # Inserir execução da rota
            execucao_id = str(uuid.uuid4())
            
            cursor.execute("""
                INSERT INTO rota_execucoes (
                    execucao_id, rota_id, motorista_id, status_final,
                    total_paradas, paradas_concluidas, peso_estimado, peso_coletado,
                    tempo_total_minutos, data_inicio, data_finalizacao
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                execucao_id,
                rota_id,
                sessao.get('motorista_id'),
                sessao.get('status'),
                len(paradas),
                paradas_concluidas,
                peso_estimado,
                peso_coletado,
                tempo_total,
                data_inicio,
                data_fim
            ))
            
            # Inserir paradas
            paradas_data = []
            for idx, parada in enumerate(paradas):
                paradas_data.append((
                    str(uuid.uuid4()),
                    execucao_id,
                    parada.get('endereco', ''),
                    parada.get('material', ''),
                    float(parada.get('peso_estimado', 0)),
                    float(parada.get('peso_real', 0)) if parada.get('peso_real') else 0,
                    parada.get('status', 'PENDENTE'),
                    parada.get('observacoes', '')
                ))
            
            if paradas_data:
                execute_values(
                    cursor,
                    """
                    INSERT INTO rota_execucoes_paradas (
                        parada_execucao_id, execucao_id, endereco, material,
                        peso_estimado, peso_coletado, status, observacoes
                    ) VALUES %s
                    """,
                    paradas_data
                )
            
            self.postgres_conn.commit()
            logger.info(f"✓ Rota {rota_id} persistida no PostgreSQL (ID: {execucao_id})")
            return True
            
        except Exception as e:
            self.postgres_conn.rollback()
            logger.error(f"✗ Erro ao persistir rota no PostgreSQL: {e}")
            return False
    
    def _limpar_redis(self, rota_id: str) -> bool:
        """Remove dados da rota do Redis após sincronização bem-sucedida"""
        try:
            sessao_key = f"rota:sessao:{rota_id}"
            parada_keys = self.redis_client.keys(f"rota:parada:{rota_id}:*")
            fila_key = f"rota:fila:{rota_id}"
            
            # Deletar chaves
            if self.redis_client.exists(sessao_key):
                self.redis_client.delete(sessao_key)
            
            for key in parada_keys:
                self.redis_client.delete(key)
            
            if self.redis_client.exists(fila_key):
                self.redis_client.delete(fila_key)
            
            logger.info(f"✓ Redis limpo para rota {rota_id}")
            return True
        except Exception as e:
            logger.error(f"✗ Erro ao limpar Redis: {e}")
            return False
    
    def sincronizar_rota_finalizada(self, rota_id: str) -> bool:
        """Sincroniza uma rota finalizada do Redis para PostgreSQL"""
        logger.info(f"→ Sincronizando rota: {rota_id}")
        
        # Obter dados do Redis
        dados = self._obter_rota_redis(rota_id)
        if not dados:
            logger.warning(f"⚠ Rota {rota_id} não encontrada no Redis")
            return False
        
        # Verificar se está finalizada
        if dados['sessao'].get('status') != 'FINALIZADA':
            logger.info(f"⚠ Rota {rota_id} ainda não está finalizada")
            return False
        
        # Persistir em PostgreSQL
        if not self._persistir_rota_postgres(rota_id, dados):
            return False
        
        # Limpar Redis
        if not self._limpar_redis(rota_id):
            logger.warning(f"⚠ Falha ao limpar Redis, mas dados foram persistidos")
        
        return True
    
    def processar_todas_rotas_finalizadas(self) -> int:
        """Processa todas as rotas finalizadas no Redis"""
        try:
            # Encontrar todas as sessões de rotas
            sessao_keys = self.redis_client.keys("rota:sessao:*")
            
            if not sessao_keys:
                logger.info("ℹ Nenhuma rota para sincronizar")
                return 0
            
            sincronizadas = 0
            
            for key in sessao_keys:
                # Extrair rota_id da chave
                rota_id = key.replace("rota:sessao:", "")
                
                # Verificar se está finalizada
                sessao = self.redis_client.hgetall(key)
                if sessao.get('status') == 'FINALIZADA':
                    if self.sincronizar_rota_finalizada(rota_id):
                        sincronizadas += 1
            
            if sincronizadas > 0:
                logger.info(f"✓ {sincronizadas} rota(s) sincronizada(s)")
            
            return sincronizadas
            
        except Exception as e:
            logger.error(f"✗ Erro ao processar rotas: {e}")
            return 0
    
    def obter_progresso_rota(self, rota_id: str) -> Optional[Dict]:
        """Obtém progresso atual de uma rota em execução"""
        try:
            sessao_key = f"rota:sessao:{rota_id}"
            if not self.redis_client.exists(sessao_key):
                return None
            
            sessao = self.redis_client.hgetall(sessao_key)
            parada_keys = self.redis_client.keys(f"rota:parada:{rota_id}:*")
            paradas = [self.redis_client.hgetall(key) for key in parada_keys]
            
            concluidas = sum(1 for p in paradas if p.get('status') == 'CONCLUIDA')
            total = len(paradas)
            
            return {
                'rota_id': rota_id,
                'status': sessao.get('status'),
                'progresso_percentual': round((concluidas / total * 100) if total > 0 else 0, 2),
                'paradas_concluidas': concluidas,
                'paradas_totais': total,
                'timestamp_atualizacao': sessao.get('timestamp_atualizacao')
            }
        except Exception as e:
            logger.error(f"✗ Erro ao obter progresso: {e}")
            return None
    
    def fechar(self) -> None:
        """Fecha conexões"""
        if self.redis_client:
            try:
                self.redis_client.close()
            except:
                pass
        
        if self.postgres_conn:
            try:
                self.postgres_conn.close()
            except:
                pass


# ============================================================================
# EXECUÇÃO
# ============================================================================

def main():
    """Função principal"""
    logger.info("=" * 60)
    logger.info("SINCRONIZADOR ROTAS REDIS → POSTGRESQL")
    logger.info("=" * 60)
    
    sincronizador = None
    
    try:
        # Inicializar
        sincronizador = SincronizadorRotasPostgreSQL()
        
        # Criar tabelas se necessário
        sincronizador._criar_tabelas()
        
        # Processar rotas finalizadas
        total = sincronizador.processar_todas_rotas_finalizadas()
        
        logger.info("=" * 60)
        logger.info(f"Execução finalizada ({total} rota(s) sincronizada(s))")
        logger.info("=" * 60)
        
    except Exception as e:
        logger.error(f"✗ Erro fatal: {e}")
        return 1
    
    finally:
        if sincronizador:
            sincronizador.fechar()
    
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())