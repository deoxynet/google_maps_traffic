use crate::osm::types::OsmEdge;
use rstar::{AABB, RTree, RTreeObject, primitives::Line};

#[derive(Debug, Clone)]
pub(super) struct IndexedEdge {
    pub(super) line: Line<[f64; 2]>,
    pub(super) edge_index: usize,
}

impl IndexedEdge {
    fn from_edge(edge: &OsmEdge, edge_index: usize) -> Self {
        Self {
            line: Line::new(
                [edge.start.lng, edge.start.lat],
                [edge.end.lng, edge.end.lat],
            ),
            edge_index,
        }
    }
}

impl RTreeObject for IndexedEdge {
    type Envelope = AABB<[f64; 2]>;

    fn envelope(&self) -> Self::Envelope {
        self.line.envelope()
    }
}

pub(super) fn build_rtree(edges: &[OsmEdge]) -> RTree<IndexedEdge> {
    let indexed = edges
        .iter()
        .enumerate()
        .map(|(idx, edge)| IndexedEdge::from_edge(edge, idx))
        .collect();
    RTree::bulk_load(indexed)
}
