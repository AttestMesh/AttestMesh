//! Rolling liveness view + first-convergence calculation (sidecar spec §11.4).
//!
//! Time is a logical millisecond clock passed in by the caller so the convergence
//! logic is deterministically testable. The send/recv loops pass real wall-clock ms.

use std::collections::{BTreeSet, HashMap};

pub type MemberId = [u8; 32];

pub struct Liveness {
    self_id: MemberId,
    interval_ms: u64,
    miss_threshold: u64,
    last_seen: HashMap<MemberId, u64>,
    connected_view: HashMap<MemberId, BTreeSet<MemberId>>,
    first_converged: bool,
}

impl Liveness {
    /// Defaults (master spec §13 item 10): 2s interval, 3-miss threshold.
    pub fn new(self_id: MemberId) -> Self {
        Self::with_params(self_id, 2000, 3)
    }

    pub fn with_params(self_id: MemberId, interval_ms: u64, miss_threshold: u64) -> Self {
        Self {
            self_id,
            interval_ms,
            miss_threshold,
            last_seen: HashMap::new(),
            connected_view: HashMap::new(),
            first_converged: false,
        }
    }

    /// Record this node's own current connected-set view. Self is always "up".
    pub fn record_self_view(&mut self, connected: &[MemberId], now_ms: u64) {
        let me = self.self_id;
        self.last_seen.insert(me, now_ms);
        self.connected_view
            .insert(me, connected.iter().copied().collect());
    }

    /// Ingest a received heartbeat from `sender`.
    pub fn on_heartbeat(&mut self, sender: MemberId, connected: &[MemberId], now_ms: u64) {
        self.last_seen.insert(sender, now_ms);
        self.connected_view
            .insert(sender, connected.iter().copied().collect());
    }

    fn window_ms(&self) -> u64 {
        self.interval_ms * self.miss_threshold
    }

    pub fn is_up(&self, id: &MemberId, now_ms: u64) -> bool {
        self.last_seen
            .get(id)
            .map(|t| now_ms.saturating_sub(*t) < self.window_ms())
            .unwrap_or(false)
    }

    /// Nodes we have a fresh view from (peers + self).
    fn active(&self, now_ms: u64) -> BTreeSet<MemberId> {
        self.last_seen
            .iter()
            .filter(|(_, t)| now_ms.saturating_sub(**t) < self.window_ms())
            .map(|(id, _)| *id)
            .collect()
    }

    /// The cluster-wide live set: every node any active node reports connected.
    pub fn live_set(&self, now_ms: u64) -> BTreeSet<MemberId> {
        let mut live = BTreeSet::new();
        for a in self.active(now_ms) {
            if let Some(view) = self.connected_view.get(&a) {
                live.extend(view.iter().copied());
            }
        }
        live
    }

    /// Converged iff every active node's view equals the live set, and the set of
    /// active nodes equals the live set (every live node is one we hear from fresh).
    pub fn is_converged(&mut self, now_ms: u64) -> bool {
        let active = self.active(now_ms);
        if active.is_empty() {
            return false;
        }
        let live = self.live_set(now_ms);
        if live.is_empty() || active != live {
            return false;
        }
        for a in &active {
            match self.connected_view.get(a) {
                Some(view) if *view == live => {}
                _ => return false,
            }
        }
        self.first_converged = true;
        true
    }

    /// The first-convergence gate: latched once, never reset (master spec §13 item 11).
    pub fn first_converged(&self) -> bool {
        self.first_converged
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(n: u8) -> MemberId {
        let mut b = [0u8; 32];
        b[31] = n;
        b
    }

    #[test]
    fn full_three_node_mesh_converges() {
        let me = id(1);
        let (b, c) = (id(2), id(3));
        let all = [me, b, c];
        let mut lv = Liveness::with_params(me, 1000, 3);

        lv.record_self_view(&all, 1000);
        lv.on_heartbeat(b, &all, 1000);
        lv.on_heartbeat(c, &all, 1000);

        assert!(lv.is_converged(1000));
        assert!(lv.first_converged());
    }

    #[test]
    fn disagreeing_views_do_not_converge() {
        let me = id(1);
        let (b, c) = (id(2), id(3));
        let mut lv = Liveness::with_params(me, 1000, 3);

        lv.record_self_view(&[me, b, c], 1000);
        lv.on_heartbeat(b, &[me, b, c], 1000);
        // c only sees itself + me (missing b).
        lv.on_heartbeat(c, &[me, c], 1000);

        assert!(!lv.is_converged(1000));
    }

    #[test]
    fn stale_peer_drops_from_up_and_breaks_convergence() {
        let me = id(1);
        let (b, c) = (id(2), id(3));
        let all = [me, b, c];
        let mut lv = Liveness::with_params(me, 1000, 3); // window = 3000ms

        lv.record_self_view(&all, 1000);
        lv.on_heartbeat(b, &all, 1000);
        lv.on_heartbeat(c, &all, 1000);
        assert!(lv.is_converged(1000));

        // 5s later, only self + b refresh; c is stale.
        lv.record_self_view(&all, 6000);
        lv.on_heartbeat(b, &all, 6000);
        assert!(!lv.is_up(&c, 6000));
        // Active = {me, b} but they still report c connected → live != active → not converged.
        assert!(!lv.is_converged(6000));
        // Gate stays latched though.
        assert!(lv.first_converged());
    }

    #[test]
    fn gate_latches_once() {
        let me = id(1);
        let mut lv = Liveness::with_params(me, 1000, 3);
        lv.record_self_view(&[me], 1000);
        assert!(lv.is_converged(1000));
        assert!(lv.first_converged());
        // Even after everything goes stale, the latch holds.
        assert!(!lv.is_converged(99999));
        assert!(lv.first_converged());
    }
}
