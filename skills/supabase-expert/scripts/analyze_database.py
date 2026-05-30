#!/usr/bin/env python3
"""
Direct Supabase Database Analysis Script
Uses environment variables for connection from .env file
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
import json

# Load environment variables
load_dotenv()

class SupabaseAnalyzer:
    def __init__(self):
        """Initialize connection using .env variables"""
        # Get Supabase connection details from environment
        self.db_url = os.getenv('SUPABASE_DB_URL') or os.getenv('DATABASE_URL')

        # Alternative: construct from individual components
        if not self.db_url:
            host = os.getenv('SUPABASE_HOST')
            port = os.getenv('SUPABASE_PORT', '5432')
            database = os.getenv('SUPABASE_DATABASE', 'postgres')
            user = os.getenv('SUPABASE_USER')
            password = os.getenv('SUPABASE_PASSWORD')

            if all([host, user, password]):
                self.db_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"

        if not self.db_url:
            raise ValueError("Database connection not configured. Check .env file")

        self.conn = None
        self.connect()

    def connect(self):
        """Establish database connection"""
        try:
            self.conn = psycopg2.connect(self.db_url)
            print("✅ Connected to Supabase database")
        except Exception as e:
            print(f"❌ Connection failed: {e}")
            raise

    def analyze_slow_queries(self):
        """Find slowest queries in the database"""
        query = """
        SELECT
            query,
            mean_exec_time::numeric(10,2) as avg_ms,
            calls,
            total_exec_time::numeric(10,2) as total_ms,
            (mean_exec_time / 1000)::numeric(10,3) as avg_seconds
        FROM pg_stat_statements
        WHERE query NOT LIKE '%pg_stat_statements%'
            AND query NOT LIKE 'COMMIT%'
            AND query NOT LIKE 'BEGIN%'
        ORDER BY mean_exec_time DESC
        LIMIT 20
        """

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(query)
                results = cur.fetchall()

                print("\n🔍 SLOWEST QUERIES")
                print("=" * 80)
                for i, row in enumerate(results, 1):
                    print(f"\n{i}. Average: {row['avg_ms']}ms | Calls: {row['calls']}")
                    print(f"   Query: {row['query'][:100]}...")

                return results
            except Exception as e:
                print(f"Note: pg_stat_statements may not be enabled: {e}")
                return []

    def check_table_sizes(self):
        """Check size of all tables"""
        query = """
        SELECT
            schemaname,
            tablename,
            pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) AS total_size,
            pg_size_pretty(pg_relation_size(schemaname||'.'||tablename)) AS table_size,
            pg_total_relation_size(schemaname||'.'||tablename) as bytes
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
        """

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            results = cur.fetchall()

            print("\n📊 TABLE SIZES")
            print("=" * 80)
            for row in results:
                print(f"{row['tablename']:30} | Total: {row['total_size']:>10} | Table: {row['table_size']:>10}")

            return results

    def find_missing_indexes(self):
        """Find foreign keys without indexes"""
        query = """
        SELECT
            c.conname AS constraint_name,
            tbl.relname AS table_name,
            col.attname AS column_name,
            'CREATE INDEX idx_' || tbl.relname || '_' || col.attname ||
            ' ON ' || tbl.relname || '(' || col.attname || ');' AS create_index_sql
        FROM pg_constraint c
        JOIN pg_class tbl ON c.conrelid = tbl.oid
        JOIN pg_attribute col ON col.attrelid = tbl.oid
            AND col.attnum = ANY(c.conkey)
        WHERE c.contype = 'f'
            AND NOT EXISTS (
                SELECT 1
                FROM pg_index i
                WHERE i.indrelid = c.conrelid
                    AND col.attnum = ANY(i.indkey)
            )
        """

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            results = cur.fetchall()

            if results:
                print("\n⚠️ MISSING INDEXES ON FOREIGN KEYS")
                print("=" * 80)
                for row in results:
                    print(f"Table: {row['table_name']}, Column: {row['column_name']}")
                    print(f"Fix: {row['create_index_sql']}\n")
            else:
                print("\n✅ All foreign keys have indexes")

            return results

    def check_connection_stats(self):
        """Check current database connections"""
        query = """
        SELECT
            datname,
            count(*) as connections,
            count(*) FILTER (WHERE state = 'active') as active,
            count(*) FILTER (WHERE state = 'idle') as idle,
            count(*) FILTER (WHERE state = 'idle in transaction') as idle_in_transaction
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY datname
        """

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query)
            result = cur.fetchone()

            print("\n🔌 CONNECTION STATISTICS")
            print("=" * 80)
            print(f"Total connections: {result['connections']}")
            print(f"Active: {result['active']}")
            print(f"Idle: {result['idle']}")
            print(f"Idle in transaction: {result['idle_in_transaction']}")

            return result

    def check_crawl_performance(self):
        """Check crawl performance metrics"""

        # Check crawl_results query performance
        query_crawl = """
        SELECT
            COUNT(*) as total_rows,
            COUNT(DISTINCT url) as unique_urls,
            AVG(EXTRACT(EPOCH FROM (updated_at - created_at))) as avg_process_time_seconds,
            MAX(created_at) as latest_crawl
        FROM crawl_results
        WHERE created_at > NOW() - INTERVAL '7 days'
        """

        # Check storage query performance
        query_storage = """
        EXPLAIN (ANALYZE, BUFFERS)
        SELECT * FROM crawl_results
        WHERE url = 'test'
        LIMIT 1
        """

        with self.conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Crawl metrics
            cur.execute(query_crawl)
            crawl_metrics = cur.fetchone()

            print("\nCRAWL PERFORMANCE METRICS")
            print("=" * 80)
            print(f"Total crawls (7 days): {crawl_metrics['total_rows']}")
            print(f"Unique URLs: {crawl_metrics['unique_urls']}")
            if crawl_metrics['avg_process_time_seconds']:
                print(f"Avg processing time: {crawl_metrics['avg_process_time_seconds']:.2f}s")
            print(f"Latest crawl: {crawl_metrics['latest_crawl']}")

            # Test query performance
            start = datetime.now()
            cur.execute(query_storage)
            query_plan = cur.fetchall()
            duration = (datetime.now() - start).total_seconds() * 1000

            print(f"\nTest query duration: {duration:.2f}ms")

            return {
                'crawl_metrics': crawl_metrics,
                'query_performance_ms': duration
            }

    def optimize_tables(self):
        """Run VACUUM and ANALYZE on main tables"""
        tables = ['crawl_results', 'rag_embeddings', 'competitor_data', 'webvitals_metrics']

        print("\n🔧 OPTIMIZING TABLES")
        print("=" * 80)

        with self.conn.cursor() as cur:
            for table in tables:
                try:
                    # Check if table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_schema = 'public'
                            AND table_name = %s
                        )
                    """, (table,))

                    if cur.fetchone()[0]:
                        print(f"Optimizing {table}...")
                        self.conn.commit()  # Commit any pending transaction
                        cur.execute(f"VACUUM ANALYZE {table}")
                        print(f"✅ {table} optimized")
                    else:
                        print(f"⏭️ {table} not found, skipping")
                except Exception as e:
                    print(f"❌ Error optimizing {table}: {e}")

    def generate_report(self):
        """Generate comprehensive database health report"""
        report = {
            'timestamp': datetime.now().isoformat(),
            'connection_stats': self.check_connection_stats(),
            'table_sizes': self.check_table_sizes(),
            'missing_indexes': self.find_missing_indexes(),
            'slow_queries': self.analyze_slow_queries()[:5],  # Top 5
            'crawl_metrics': self.check_crawl_performance()
        }

        # Save report
        report_file = f"supabase_health_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        print(f"\n📄 Report saved to: {report_file}")
        return report

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
            print("\n👋 Connection closed")

def main():
    """Run complete database analysis"""
    print("🚀 SUPABASE DATABASE ANALYZER")
    print("=" * 80)

    analyzer = SupabaseAnalyzer()

    try:
        # Run all analyses
        analyzer.check_connection_stats()
        analyzer.check_table_sizes()
        analyzer.find_missing_indexes()
        analyzer.analyze_slow_queries()
        analyzer.check_crawl_performance()

        # Optional: optimize tables (be careful in production!)
        response = input("\n⚠️ Run VACUUM ANALYZE on tables? (y/n): ")
        if response.lower() == 'y':
            analyzer.optimize_tables()

        # Generate report
        analyzer.generate_report()

    finally:
        analyzer.close()

if __name__ == "__main__":
    main()