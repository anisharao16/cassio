"""
A common driver to operate on tables with vector-similarity-search indices.
"""

import json
from operator import itemgetter
from typing import List, Optional, Union, Dict, Any

from cassandra.cluster import Session
from cassandra.query import SimpleStatement

import cassio.cql
from cassio.utils.vector.distance_metrics import distance_metrics


JSONType = Union[Dict[str, Any], List[Any], int, float, str, bool, None]


class VectorMixin:
    def _create_index(self):
        index_name = f'{self.table}_embedding_idx'
        st = SimpleStatement(cassio.cql.create_vector_table_index.format(
            indexName=index_name,
            keyspace=self.keyspace,
            table=self.table
        ))
        self._execute_cql(st, tuple())

    def ann_search(self, embedding_vector, numRows):
        st = SimpleStatement(cassio.cql.search_vector_table_item.format(
            keyspace=self.keyspace,
            table=self.table
        ))
        return self._execute_cql(st, (embedding_vector, numRows))

    def _count_rows(self):
        st = SimpleStatement(cassio.cql.count_rows.format(
            keyspace=self.keyspace,
            table=self.table
        ))
        return self._execute_cql(st, tuple()).one().count


class VectorTable(VectorMixin):

    def __init__(self, session: Session, keyspace: str, table: str, embedding_dimension: int, auto_id: bool):
        self.session = session
        self.keyspace = keyspace
        self.table = table
        self.embedding_dimension = embedding_dimension
        #
        self.auto_id = auto_id
        #
        self._create_table()
        self._create_index()

    def put(self,
            document: str,
            embedding_vector:
            List[float],
            document_id: Optional[str],
            metadata: JSONType,
            ttl_seconds: int):
        # document_id, if not autoID, must be str
        if not self.auto_id and document_id is None:
            raise ValueError('\'document_id\' must be specified unless autoID')
        if self.auto_id and document_id is not None:
            raise ValueError('\'document_id\' cannot be passes if autoID')
        if ttl_seconds:
            ttl_spec = f' USING TTL {ttl_seconds}'
        else:
            ttl_spec = ''
        st = SimpleStatement(cassio.cql.store_cached_vss_item.format(
            keyspace=self.keyspace,
            table=self.table,
            documentIdPlaceholder='now()' if self.auto_id else '%s',
            ttlSpec=ttl_spec,
        ))
        metadata_blob = json.dumps(metadata)
        # depending on autoID, the size of the values tuple changes:
        values0 = (embedding_vector, document, metadata_blob)
        values = values0 if self.auto_id else tuple([document_id] + list(values0))
        self._execute_cql(st, values)

    def get(self, document_id: str):
        if self.auto_id:
            raise ValueError('\'get\' not supported if autoID')
        else:
            st = SimpleStatement(cassio.cql.get_vector_table_item.format(
                keyspace=self.keyspace,
                table=self.table,
            ))
            hits = self._execute_cql(st, (document_id, ))
            hit = hits.one()
            if hit:
                return VectorTable._jsonify_hit(hit, distance=None)
            else:
                return None

    def delete(self, document_id: str) -> None:
        """This operation goes through even if the row does not exist."""
        st = SimpleStatement(cassio.cql.delete_vector_table_item.format(
            keyspace=self.keyspace,
            table=self.table,
        ))
        self._execute_cql(st, (document_id, ))

    def search(self,
               embedding_vector: List[float],
               top_k: int,
               metric: str,
               metric_threshold: float):
        # get rows by ANN
        rows = list(self.ann_search(embedding_vector, top_k))
        if not rows:
            return []
        # sort, cut, validate and prepare for returning
        #
        # evaluate metric
        distance_function, distance_reversed = distance_metrics[metric]
        row_embeddings = [
            row.embedding_vector
            for row in rows
        ]
        # enrich with their metric score
        rows_with_metric = list(zip(
            distance_function(row_embeddings, embedding_vector),
            rows,
        ))
        # sort rows by metric score. First handle metric/threshold
        if metric_threshold is not None:
            if distance_reversed:
                def _thresholder(mtx, thr): return mtx >= thr
            else:
                def _thresholder(mtx, thr): return mtx <= thr
        else:
            # no hits are discarded
            def _thresholder(mtx, thr): return True
        #
        sorted_passing_winners = sorted(
            (pair for pair in rows_with_metric if _thresholder(pair[0], metric_threshold)),
            key=itemgetter(0),
            reverse=distance_reversed,
        )
        # we discard the scores and return an iterable of hits (as JSON)
        return [
            VectorTable._jsonify_hit(hit, distance)
            for distance, hit in sorted_passing_winners
        ]

    @staticmethod
    def _jsonify_hit(hit, distance: float):
        d = {
            'document_id': hit.document_id,
            'metadata': json.loads(hit.metadata_blob),
            'document': hit.document,
            'embedding_vector': hit.embedding_vector,
        }
        if distance is not None:
            d['distance'] = distance
        return d

    def clear(self):
        st = SimpleStatement(cassio.cql.truncate_vector_table.format(
            keyspace=self.keyspace,
            table=self.table,
        ))
        self._execute_cql(st, tuple())

    def _create_table(self):
        st = SimpleStatement(cassio.cql.create_vector_table.format(
            keyspace=self.keyspace,
            table=self.table,
            idType='UUID' if self.auto_id else 'TEXT',
            embeddingDimension=self.embedding_dimension,
        ))
        self._execute_cql(st, tuple())

    def _execute_cql(self, statement, params: tuple):
        return self.session.execute(statement, params)
