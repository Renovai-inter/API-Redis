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

from dotenv import load_dotenv
from redis_connection import conectar_redis

load_dotenv()

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

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


def _indice_parada(chave: str) -> int:
    try:
        return int(chave.rsplit(':', 1)[1])
    except (IndexError, ValueError):
        return 1 << 30


def _uuid_ou_none(valor) -> Optional[str]:
    """endereco_id só vai para a coluna UUID se for um UUID válido (um texto qualquer derrubaria o insert)."""
    try:
        return str(uuid.UUID(str(valor))) if valor else None
    except ValueError:
        return None


def _data_ou_none(valor) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(valor) if valor else None
    except ValueError:
        return None

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
            self.redis_client = conectar_redis()
            if self.redis_client is None:
                raise redis.ConnectionError('Redis indisponível')
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

            # Colunas novas (só acrescenta, não mexe no que já existe): posição da parada na
            # viagem, o endereço da rota (rotas_enderecos.endereco_id) e a hora em que a coleta
            # foi concluída — o RPA-neo4j usa para calcular o tempo real de cada trecho.
            cursor.execute("""
                ALTER TABLE rota_execucoes_paradas
                    ADD COLUMN IF NOT EXISTS ordem INTEGER,
                    ADD COLUMN IF NOT EXISTS endereco_id UUID,
                    ADD COLUMN IF NOT EXISTS data_conclusao TIMESTAMP;
            """)

            # Tabela de historico de acessos diarios de usuarios (DAU)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS atividade_usuarios_diaria (
                    atividade_id   UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
                    data_acesso    DATE         NOT NULL,
                    tipo_usuario   VARCHAR(20)  NOT NULL CHECK (tipo_usuario IN ('COOPERADO', 'EMPRESA')),
                    funcionario_id UUID,
                    empresa_id     UUID,
                    criado_em      TIMESTAMP    DEFAULT NOW(),

                    CONSTRAINT uq_atividade_usuario_dia UNIQUE (data_acesso, tipo_usuario, funcionario_id, empresa_id)
                );
            """)

            # Tabela de metricas consolidadas de DAU para o Data Mart e BI
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metricas_dau (
                    data_referencia         DATE         PRIMARY KEY,
                    total_cooperados_ativos INTEGER      NOT NULL DEFAULT 0,
                    total_empresas_ativas   INTEGER      NOT NULL DEFAULT 0,
                    total_usuarios_ativos   INTEGER      NOT NULL DEFAULT 0,
                    atualizado_em           TIMESTAMP    DEFAULT NOW()
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
            
            for key in sorted(parada_keys, key=_indice_parada):
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
                    parada.get('observacoes', ''),
                    int(parada.get('id', idx)),
                    _uuid_ou_none(parada.get('endereco_id')),
                    _data_ou_none(parada.get('timestamp_conclusao')),
                ))
            
            if paradas_data:
                execute_values(
                    cursor,
                    """
                    INSERT INTO rota_execucoes_paradas (
                        parada_execucao_id, execucao_id, endereco, material,
                        peso_estimado, peso_coletado, status, observacoes,
                        ordem, endereco_id, data_conclusao
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

        # Já gravada: se a limpeza abaixo falhar, o próximo ciclo não grava a mesma viagem de novo.
        try:
            self.redis_client.hset(f"rota:sessao:{rota_id}", 'status', 'SINCRONIZADA')
        except Exception as e:
            logger.warning(f"⚠ Não foi possível marcar a rota {rota_id} como sincronizada: {e}")
        
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
                elif sessao.get('status') == 'SINCRONIZADA':
                    # gravada no Postgres, mas a limpeza falhou antes: só limpa
                    self._limpar_redis(rota_id)
            
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
    
    def sincronizar_dau_postgres(self) -> int:
        """Sincroniza registros de atividade e calcula metricas DAU no PostgreSQL a partir do Redis."""
        if not self.redis_client or not self.postgres_conn:
            return 0

        total_processados = 0
        try:
            chaves_pendentes = self.redis_client.keys('dau:pendentes:*')
            if not chaves_pendentes:
                return 0

            cursor = self.postgres_conn.cursor()

            for chave in chaves_pendentes:
                chave_str = chave.decode('utf-8') if isinstance(chave, bytes) else str(chave)
                partes = chave_str.split(':')
                if len(partes) < 3:
                    continue
                data_str = partes[2]  # YYYY-MM-DD
                try:
                    data_referencia = datetime.strptime(data_str, '%Y-%m-%d').date()
                except ValueError:
                    continue

                membros = self.redis_client.smembers(chave)
                if not membros:
                    self.redis_client.delete(chave)
                    continue

                dados_inserir = []
                ids_funcionarios = []

                for m in membros:
                    m_str = m.decode('utf-8') if isinstance(m, bytes) else str(m)
                    if ':' not in m_str:
                        continue
                    tipo, ent_id = m_str.split(':', 1)
                    tipo = tipo.upper()
                    uuid_validado = _uuid_ou_none(ent_id)
                    if not uuid_validado:
                        continue

                    if tipo == 'COOPERADO':
                        dados_inserir.append((data_referencia, tipo, uuid_validado, None))
                        ids_funcionarios.append(uuid_validado)
                    elif tipo == 'EMPRESA':
                        dados_inserir.append((data_referencia, tipo, None, uuid_validado))

                if dados_inserir:
                    query_insert = """
                        INSERT INTO atividade_usuarios_diaria 
                            (data_acesso, tipo_usuario, funcionario_id, empresa_id)
                        VALUES %s
                        ON CONFLICT (data_acesso, tipo_usuario, funcionario_id, empresa_id) DO NOTHING;
                    """
                    execute_values(cursor, query_insert, dados_inserir)

                    if ids_funcionarios:
                        cursor.execute("""
                            UPDATE usuarios u
                            SET ultimo_acesso = NOW()
                            FROM funcionarios f
                            WHERE f.usuario_id = u.usuario_id
                              AND f.funcionario_id = ANY(%s::uuid[]);
                        """, (ids_funcionarios,))

                    cursor.execute("""
                        INSERT INTO metricas_dau (
                            data_referencia,
                            total_cooperados_ativos,
                            total_empresas_ativas,
                            total_usuarios_ativos,
                            atualizado_em
                        )
                        SELECT 
                            %s,
                            COUNT(DISTINCT funcionario_id) FILTER (WHERE tipo_usuario = 'COOPERADO'),
                            COUNT(DISTINCT empresa_id)     FILTER (WHERE tipo_usuario = 'EMPRESA'),
                            COUNT(DISTINCT COALESCE(funcionario_id, empresa_id)),
                            NOW()
                        FROM atividade_usuarios_diaria
                        WHERE data_acesso = %s
                        ON CONFLICT (data_referencia) DO UPDATE
                        SET total_cooperados_ativos = EXCLUDED.total_cooperados_ativos,
                            total_empresas_ativas   = EXCLUDED.total_empresas_ativas,
                            total_usuarios_ativos   = EXCLUDED.total_usuarios_ativos,
                            atualizado_em           = NOW();
                    """, (data_referencia, data_referencia))

                    total_processados += len(dados_inserir)

                self.postgres_conn.commit()
                self.redis_client.delete(chave)
                logger.info(f"✓ DAU para {data_referencia} sincronizado: {len(dados_inserir)} registro(s).")

            return total_processados

        except Exception as e:
            if self.postgres_conn:
                self.postgres_conn.rollback()
            logger.error(f"✗ Erro ao sincronizar DAU para PostgreSQL: {e}")
            return 0

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
