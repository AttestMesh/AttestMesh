//! The two health gates (sidecar spec §7.1 steps 10–11, §14): first-convergence
//! and CSK-acquired. The sidecar reports healthy only once BOTH are set; the
//! first-convergence gate latches once and never resets (master spec §13 item 11).

use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Default)]
pub struct Gates {
    first_converged: AtomicBool,
    csk_acquired: AtomicBool,
}

impl Gates {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn latch_first_converged(&self) {
        self.first_converged.store(true, Ordering::SeqCst);
    }

    pub fn set_csk_acquired(&self) {
        self.csk_acquired.store(true, Ordering::SeqCst);
    }

    pub fn first_converged(&self) -> bool {
        self.first_converged.load(Ordering::SeqCst)
    }

    pub fn csk_acquired(&self) -> bool {
        self.csk_acquired.load(Ordering::SeqCst)
    }

    /// Healthy iff both gates are open.
    pub fn healthy(&self) -> bool {
        self.first_converged() && self.csk_acquired()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn healthy_requires_both_gates() {
        let g = Gates::new();
        assert!(!g.healthy());
        g.latch_first_converged();
        assert!(!g.healthy());
        g.set_csk_acquired();
        assert!(g.healthy());
    }
}
