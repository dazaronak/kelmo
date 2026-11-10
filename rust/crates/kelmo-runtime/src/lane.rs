//! One bounded mailbox per core and lane: at most `limit` calls in flight, at most `queue` waiting.

use std::collections::{HashMap, VecDeque};

use crate::spec::{LaneSpec, Order};

/// A queued call, as far as the mailbox cares: its router id and the origin it is fair across.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    pub id: u64,
    pub origin: String,
}

#[derive(Debug)]
pub struct Lane {
    pub limit: usize,
    pub capacity: usize,
    pub in_flight: usize,
    order: Order,
    fifo: VecDeque<Entry>,
    by_origin: HashMap<String, VecDeque<Entry>>,
    turn: VecDeque<String>,
    queued: usize,
}

impl Lane {
    pub fn new(spec: &LaneSpec) -> Self {
        Lane {
            limit: spec.limit,
            capacity: spec.queue,
            in_flight: 0,
            order: spec.order,
            fifo: VecDeque::new(),
            by_origin: HashMap::new(),
            turn: VecDeque::new(),
            queued: 0,
        }
    }

    pub fn queued(&self) -> usize {
        self.queued
    }

    /// Queues an entry, or gives it back when the queue is full.
    pub fn push(&mut self, entry: Entry) -> Result<(), Entry> {
        if self.queued >= self.capacity {
            return Err(entry);
        }
        self.insert(entry, false);
        Ok(())
    }

    /// Puts an entry back at the head of its queue, past the bound: a call re-dispatched after a restart
    /// was admitted once already.
    pub fn push_front(&mut self, entry: Entry) {
        self.insert(entry, true);
    }

    fn insert(&mut self, entry: Entry, front: bool) {
        self.queued += 1;
        match self.order {
            Order::Fifo if front => self.fifo.push_front(entry),
            Order::Fifo => self.fifo.push_back(entry),
            Order::RoundRobin => {
                let origin = entry.origin.clone();
                let queue = self.by_origin.entry(origin.clone()).or_default();
                if queue.is_empty() && !self.turn.contains(&origin) {
                    if front {
                        self.turn.push_front(origin);
                    } else {
                        self.turn.push_back(origin);
                    }
                }
                if front {
                    queue.push_front(entry);
                } else {
                    queue.push_back(entry);
                }
            }
        }
    }

    /// The next entry to dispatch, if a slot is free.
    pub fn take(&mut self) -> Option<Entry> {
        if self.in_flight >= self.limit || self.queued == 0 {
            return None;
        }
        let entry = match self.order {
            Order::Fifo => self.fifo.pop_front(),
            Order::RoundRobin => {
                let origin = self.turn.pop_front()?;
                let queue = self.by_origin.get_mut(&origin)?;
                let entry = queue.pop_front();
                if queue.is_empty() {
                    self.by_origin.remove(&origin);
                } else {
                    self.turn.push_back(origin);
                }
                entry
            }
        }?;
        self.queued -= 1;
        self.in_flight += 1;
        Some(entry)
    }

    /// Removes a queued entry (cancelled or expired). False when it is not queued here.
    pub fn remove(&mut self, id: u64) -> bool {
        let removed = match self.order {
            Order::Fifo => match self.fifo.iter().position(|entry| entry.id == id) {
                Some(index) => self.fifo.remove(index).is_some(),
                None => false,
            },
            Order::RoundRobin => {
                let mut found = None;
                for (origin, queue) in self.by_origin.iter_mut() {
                    if let Some(index) = queue.iter().position(|entry| entry.id == id) {
                        queue.remove(index);
                        found = Some((origin.clone(), queue.is_empty()));
                        break;
                    }
                }
                match found {
                    Some((origin, empty)) => {
                        if empty {
                            self.by_origin.remove(&origin);
                            self.turn.retain(|item| item != &origin);
                        }
                        true
                    }
                    None => false,
                }
            }
        };
        if removed {
            self.queued -= 1;
        }
        removed
    }

    pub fn finished(&mut self) {
        self.in_flight = self.in_flight.saturating_sub(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lane(order: Order, limit: usize, queue: usize) -> Lane {
        Lane::new(&LaneSpec {
            limit,
            queue,
            order,
        })
    }

    fn entry(id: u64, origin: &str) -> Entry {
        Entry {
            id,
            origin: origin.into(),
        }
    }

    #[test]
    fn round_robin_alternates_origins_and_keeps_each_origin_in_order() {
        let mut lane = lane(Order::RoundRobin, 10, 10);
        for (id, origin) in [(1, "a"), (2, "a"), (3, "a"), (4, "b"), (5, "b")] {
            lane.push(entry(id, origin)).unwrap();
        }
        let order: Vec<u64> = std::iter::from_fn(|| lane.take()).map(|e| e.id).collect();
        assert_eq!(order, [1, 4, 2, 5, 3]);
    }

    #[test]
    fn the_limit_holds_and_the_queue_refuses_past_its_bound() {
        let mut lane = lane(Order::Fifo, 1, 2);
        lane.push(entry(1, "a")).unwrap();
        lane.push(entry(2, "a")).unwrap();
        assert!(lane.push(entry(3, "a")).is_err());
        assert_eq!(lane.take().map(|e| e.id), Some(1));
        assert_eq!(lane.take(), None, "one in flight");
        lane.finished();
        assert_eq!(lane.take().map(|e| e.id), Some(2));
    }

    #[test]
    fn removing_a_queued_entry_frees_its_place() {
        let mut lane = lane(Order::RoundRobin, 1, 2);
        lane.push(entry(1, "a")).unwrap();
        lane.push(entry(2, "b")).unwrap();
        assert!(lane.remove(1));
        assert!(!lane.remove(1));
        assert_eq!(lane.queued(), 1);
        assert_eq!(lane.take().map(|e| e.id), Some(2));
    }
}
