# =============================================================================
# 2013+ Copyright (c) Kirill Smorodinnikov <shaitkir@gmail.com>
# All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# =============================================================================

"""
Deep Merge recovery type - recovers keys in one hash ring (aka group)
by placing them to the node where they belong.

 * Iterate all node in the group for ranges which are not belong to it.
 * Get all keys which shouldn't be on the node:
 * Looks up keys meta info on the proper node
 * If the key on the proper node is missed or older
 * then moved it form the node to ther proper node
 * If the key is valid then just remove it from the node.
"""

import sys
import logging

from itertools import groupby
from multiprocessing import Pool

from ..etime import Time
from ..utils.misc import elliptics_create_node, worker_init, RecoverStat, LookupDirect, RemoveDirect
from ..route import RouteList
from ..iterator import Iterator
from ..range import IdRange

# XXX: change me before BETA
sys.path.insert(0, "bindings/python/")
import elliptics

log = logging.getLogger(__name__)


class Recovery(object):
    def __init__(self, key, timestamp, size, address, group, ctx, node, check=True, callback=None):
        self.key = key
        self.key_timestamp = timestamp
        self.address = address
        self.group = group
        self.node = node
        self.direct_session = elliptics.Session(node)
        self.direct_session.set_direct_id(*self.address)
        self.direct_session.groups = [group]
        self.session = elliptics.Session(node)
        self.session.groups = [group]
        self.ctx = ctx
        self.stats = RecoverStat()
        self.result = True
        self.attempt = 0
        self.total_size = size
        self.recovered_size = 0
        self.just_remove = False
        self.chunked = self.total_size > self.ctx.chunk_size
        self.check = check
        self.callback = callback
        log.debug("Created Recovery object for key: {0}, node: {1}".format(repr(key), address))

    def run(self):
        log.debug("Recovering key: {0}, node: {1}"
                  .format(repr(self.key), self.address))
        address = self.session.lookup_address(self.key, self.group)
        if address == self.address:
            log.warning("Key: {0} already on the right node: {1}"
                        .format(repr(self.key), self.address))
            self.stats.skipped += 1
            return
        else:
            log.debug("Key: {0} should be on node: {1}"
                      .format(repr(self.key), address))
        self.dest_address = address
        if self.check:
            log.debug("Lookup key: {0} on node: {1}".format(repr(self.key), self.dest_address))
            self.lookup_result = LookupDirect(self.dest_address, self.key, self.group, self.ctx, self.node, self.onlookup)
            self.lookup_result.run()
        elif self.ctx.dry_run:
            log.debug("Dry-run mode is turned on. Skipping reading, writing and removing stages.")
        else:
            self.attempt = 0
            self.read()

    def read(self):
        size = 0
        try:
            log.debug("Reading key: {} from node: {}, chunked: {}"
                      .format(repr(self.key), self.address, self.chunked))
            if self.chunked:
                size = min(self.total_size - self.recovered_size, self.ctx.chunk_size)
            if self.recovered_size != 0:
                self.direct_session.ioflags |= elliptics.io_flags.nocsum
            self.read_result = self.direct_session.read_data(self.key,
                                                             offset=self.recovered_size,
                                                             size=size)
            self.read_result.connect(self.onread)
        except Exception, e:
            log.error("Read key:{} by offset: {} and size: {} raised exception: {}"
                      .format(self.key, self.recovered_size, size, repr(e)))
            self.result = False

    def write(self):
        try:
            log.debug("Writing key: {0} to node: {1}".format(repr(self.key),
                                                             self.dest_address))
            if self.chunked:
                if self.recovered_size == 0:
                    self.write_result = self.session.write_prepare(key=self.key,
                                                                   data=self.write_data,
                                                                   remote_offset=self.recovered_size,
                                                                   psize=self.total_size)
                elif self.recovered_size + self.write_size < self.total_size:
                    self.write_result = self.session.write_plain(key=self.key,
                                                                 data=self.write_data,
                                                                 remote_offset=self.recovered_size)
                else:
                    self.write_result = self.session.write_commit(key=self.key,
                                                                  data=self.write_data,
                                                                  remote_offset=self.recovered_size,
                                                                  csize=self.total_size)
            else:
                self.write_result = self.session.write_data(key=self.key,
                                                            data=self.write_data,
                                                            offset=self.recovered_size)
            self.write_result.connect(self.onwrite)
        except Exception, e:
            log.error("Write exception: {}".format(repr(e)))
            self.result = False
            raise e

    def remove(self):
        if not self.ctx.safe:
            log.debug("Removing key: {0} from node: {1}".format(repr(self.key), self.address))
            self.remove_result = RemoveDirect(self.address,
                                              self.key,
                                              self.group,
                                              self.ctx,
                                              self.node,
                                              self.onremove)
            self.remove_result.run()
        elif self.callback:
            self.callback(self.result, self.stats)

    def onlookup(self, result, stats):
        self.lookup_result = None
        try:
            self.stats += stats
            if result and self.key_timestamp < result.timestamp:
                self.just_remove = True
                log.debug("Key: {0} on node: {1} is newer. Just removing it from node: {2}."
                          .format(repr(self.key), self.dest_address, self.address))
                if self.ctx.dry_run:
                    log.debug("Dry-run mode is turned on. Skipping removing stage.")
                    return
                self.attempt = 0
                self.remove()
                return

            log.debug("Key: {0} on node: {1} is older or miss. Reading it from node: {2}"
                      .format(repr(self.key), self.dest_address, self.address))
            if self.ctx.dry_run:
                log.debug("Dry-run mode is turned on. Skipping reading, writing and removing stages.")
                return
            self.attempt = 0
            self.read()
        except Exception as e:
            log.error("Onlookup exception: {}".format(repr(e)))
            self.result = False
            if self.callback:
                self.callback(self.result, self.stats)

    def onread(self, results, error):
        self.read_result = None
        try:
            if error.code or len(results) < 1:
                log.debug("Read key: {0} on node: {1} has been timed out: {2}"
                          .format(repr(self.key), self.address, error))
                if self.attempt < self.ctx.attempts:
                    old_timeout = self.session.timeout
                    self.session.timeout *= 2
                    self.attempt += 1
                    log.debug("Retry to read key: {0} attempt: {1}/{2} "
                              "increased timeout: {3}/{4}"
                              .format(repr(self.key), self.attempt,
                                      self.ctx.attempts,
                                      self.direct_session.timeout, old_timeout))
                    self.read()
                    self.stats.read_retries += 1
                    return
                log.error("Reading key: {0} on the node: {1} failed. "
                          "Skipping it: {2}"
                          .format(repr(self.key),
                                  self.address, error))
                self.result = False
                self.stats.read_failed += 1
                return

            if self.recovered_size == 0:
                self.session.user_flags = results[0].user_flags
                self.timestamp = results[0].timestamp
            self.stats.read += 1
            self.write_data = results[0].data
            self.write_size = results[0].size
            self.total_size = results[0].io_attribute.total_size
            self.stats.read_bytes += results[0].size
            self.attempt = 0
            self.write()
        except Exception as e:
            log.error("Onread exception: {}".format(repr(e)))
            self.result = False
            if self.callback:
                self.callback(self.result, self.stats)

    def onwrite(self, results, error):
        self.write_result = None
        try:
            if error.code or len(results) < 1:
                log.debug("Write key: {0} on node: {1} has been timed out: {2}"
                          .format(repr(self.key),
                                  self.dest_address, error))
                if self.attempt < self.ctx.attempts:
                    old_timeout = self.session.timeout
                    self.session.timeout *= 2
                    self.attempt += 1
                    log.debug("Retry to write key: {0} attempt: {1}/{2} "
                              "increased timeout: {3}/{4}"
                              .format(repr(self.key),
                                      self.attempt, self.ctx.attempts,
                                      self.direct_session.timeout, old_timeout))
                    self.stats.write_retries += 1
                    self.write()
                    return
                log.error("Writing key: {0} to node: {1} failed. "
                          "Skipping it: {2}"
                          .format(repr(self.key),
                                  self.dest_address, error))
                self.result = False
                self.stats.write_failed += 1
                return

            self.stats.write += 1
            self.stats.written_bytes += self.write_size
            self.recovered_size += self.write_size
            self.attempt = 0

            if self.recovered_size < self.total_size:
                self.read()
            else:
                log.debug("Key: {0} has been copied to node: {1}. So we can delete it from node: {2}"
                          .format(repr(self.key), self.dest_address, self.address))
                self.remove()
        except Exception as e:
            log.error("Onwrite exception: {}".format(repr(e)))
            self.result = False
            if self.callback:
                self.callback(self.result, self.stats)

    def onremove(self, removed, stats):
        self.remove_result = None
        self.result &= removed
        self.stats += stats
        if self.callback:
            self.callback(self.result, self.stats)

    def wait(self):
        log.debug("Waiting lookup for key: {0}".format(repr(self.key)))
        while hasattr(self, 'lookup_result') and self.lookup_result is not None:
            try:
                self.lookup_result.wait()
            except:
                pass
        log.debug("Lookup completed for key: {0}".format(repr(self.key)))

        log.debug("Waiting read for key: {0}".format(repr(self.key)))
        while hasattr(self, 'read_result') and self.read_result is not None:
            try:
                self.read_result.wait()
            except:
                pass
        log.debug("Read completed for key: {0}".format(repr(self.key)))

        log.debug("Waiting write for key: {0}".format(repr(self.key)))
        while hasattr(self, 'write_result') and self.write_result is not None:
            try:
                self.write_result.wait()
            except:
                pass
        log.debug("Write completed for key: {0}".format(repr(self.key)))

        log.debug("Waiting remove for key: {0}".format(repr(self.key)))
        while hasattr(self, 'remove_result') and self.remove_result is not None:
            try:
                self.remove_result.wait()
            except:
                pass
        log.debug("Remove completed for key: {0}".format(repr(self.key)))

    def succeeded(self):
        self.wait()
        return self.result


def iterate_node(ctx, node, address, ranges, eid, stats):
    try:
        log.debug("Running iterator on node: {0}".format(address))
        timestamp_range = ctx.timestamp.to_etime(), Time.time_max().to_etime()
        key_ranges = [IdRange(r[0], r[1]) for r in ranges]
        result, result_len = Iterator.iterate_with_stats(node=node,
                                                         eid=eid,
                                                         timestamp_range=timestamp_range,
                                                         key_ranges=key_ranges,
                                                         tmp_dir=ctx.tmp_dir,
                                                         address=address,
                                                         batch_size=ctx.batch_size,
                                                         stats=stats,
                                                         leave_file=False)
        if result is None:
            return None
        log.info("Iterator {0} obtained: {1} record(s)"
                 .format(result.address, result_len))
        stats.counter('iterations', 1)
        return result
    except Exception as e:
        log.error("Iteration failed for: {}: {}".format(address, repr(e)))
        stats.counter('iterations', -1)
        return None


def recover(ctx, address, group, node, results, stats):
    if results is None or len(results) < 1:
        log.warning("Recover skipped iterator results are empty for node: {0}"
                    .format(address))
        return True

    ret = True
    for batch_id, batch in groupby(enumerate(results), key=lambda x: x[0] / ctx.batch_size):
        recovers = []
        rs = RecoverStat()
        for _, response in batch:
            rec = Recovery(key=response.key,
                           timestamp=response.timestamp,
                           size=response.size,
                           address=address,
                           group=group,
                           ctx=ctx,
                           node=node)
            rec.run()
            recovers.append(rec)
        for r in recovers:
            ret &= r.succeeded()
            rs += r.stats
        rs.apply(stats)
    return ret


def process_node(address, group, ranges):
    log.debug("Processing node: {0} from group: {1} for ranges: {2}"
              .format(address, group, ranges))
    ctx = g_ctx
    stats = ctx.monitor.stats['node_{0}'.format(address)]
    stats.timer('process', 'started')

    node = elliptics_create_node(address=ctx.address,
                                 elog=ctx.elog,
                                 wait_timeout=ctx.wait_timeout,
                                 remotes=ctx.remotes,
                                 io_thread_num=4)
    s = elliptics.Session(node)

    stats.timer('process', 'iterate')
    results = iterate_node(ctx=ctx,
                           node=node,
                           address=address,
                           ranges=ranges,
                           eid=s.routes.get_address_eid(address),
                           stats=stats)
    if results is None or len(results) == 0:
        log.warning('Iterator result is empty, skipping')
        return True

    stats.timer('process', 'recover')
    ret = recover(ctx, address, group, node, results, stats)
    stats.timer('process', 'finished')

    return ret


def get_ranges(ctx, group):
    ranges = dict()
    routes = RouteList(ctx.routes.filter_by_group_id(group))

    ID_MIN = elliptics.Id([0] * 64, group)
    ID_MAX = elliptics.Id([255] * 64, group)

    addresses = None
    if ctx.one_node:
        if ctx.address not in routes.addresses():
            return None
        addresses = [ctx.address]
    else:
        addresses = routes.addresses()

    for addr in addresses:
        addr_ranges = routes.get_address_ranges(addr)
        if len(addr_ranges) == 0:
            continue

        ranges[addr] = []
        if addr_ranges[0][0] != ID_MIN:
            ranges[addr].append((ID_MIN, addr_ranges[0][0]))

        for i in xrange(1, len(addr_ranges)):
            ranges[addr].append((addr_ranges[i - 1][1], addr_ranges[i][0]))

        if addr_ranges[-1][1] != ID_MAX:
            ranges[addr].append((addr_ranges[-1][1], ID_MAX))

    return ranges


def main(ctx):
    global g_ctx
    g_ctx = ctx
    g_ctx.monitor.stats.timer('main', 'started')
    processes = min(g_ctx.nprocess, len(g_ctx.routes.addresses()))
    log.info("Creating pool of processes: {0}".format(processes))
    pool = Pool(processes=processes, initializer=worker_init)
    ret = True
    if ctx.one_node:
        ctx.groups = [ctx.address.group_id]
    for group in ctx.groups:
        log.warning("Processing group: {0}".format(group))
        group_stats = g_ctx.monitor.stats['group_{0}'.format(group)]
        group_stats.timer('group', 'started')

        ranges = get_ranges(ctx, group)

        if ranges is None:
            log.warning("There is no ranges in group: {0}, skipping this group".format(group))
            group_stats.timer('group', 'finished')
            continue

        pool_results = []

        log.debug("Processing nodes ranges: {0}".format(ranges))

        for range in ranges:
            pool_results.append(pool.apply_async(process_node, (range, group, ranges[range])))

        try:
            log.info("Fetching results")
            # Use INT_MAX as timeout, so we can catch Ctrl+C
            timeout = 2147483647
            for p in pool_results:
                ret &= p.get(timeout)
        except KeyboardInterrupt:
            log.error("Caught Ctrl+C. Terminating.")
            pool.terminate()
            pool.join()
            group_stats.timer('group', 'finished')
            g_ctx.monitor.stats.timer('main', 'finished')
            return False
        except Exception as e:
            log.error("Caught unexpected exception: {}".format(repr(e)))
            log.info("Closing pool, joining threads.")
            pool.close()
            pool.join()
            group_stats.timer('group', 'finished')
            g_ctx.monitor.stats.timer('main', 'finished')
            return False

        group_stats.timer('group', 'finished')

    log.info("Closing pool, joining threads.")
    pool.close()
    pool.join()
    g_ctx.monitor.stats.timer('main', 'finished')
    return ret


class DumpRecover(object):
    def __init__(self, routes, node, id, group, ctx):
        self.node = node
        self.id = id
        self.routes = routes
        self.group = group
        self.ctx = ctx
        simple_session = elliptics.Session(node)
        self.address = simple_session.lookup_address(self.id, group)
        self.async_lookups = []
        self.async_removes = []
        self.recover_address = None
        self.stats = RecoverStat()
        self.result = True

    def run(self):
        self.lookup_results = []
        for addr in self.routes.addresses():
            self.async_lookups.append(LookupDirect(addr, self.id, self.group, self.ctx, self.node, self.onlookup))
            self.async_lookups[-1].run()

    def onlookup(self, result, stats):
        self.stats += stats
        self.lookup_results.append(result)
        if len(self.lookup_results) == len(self.async_lookups):
            self.check()
            self.async_lookups = None

    def check(self):
        max_ts = max([r.timestamp for r in self.lookup_results if r])
        log.debug("Max timestamp of key: {}: {}".format(repr(self.id), max_ts))
        results = [r for r in self.lookup_results if r and r.timestamp == max_ts]
        max_size = max([r.size for r in results])
        log.debug("Max size of latest replicas for key: {}: {}".format(repr(self.id), max_size))
        results = [r.address for r in results if r.size == max_size]
        if self.address in results:
            log.debug("Node: {} already has the latest version of key:{}."
                      .format(self.address, repr(self.id), self.group))
            self.remove()
        else:
            self.timestamp = max_ts
            self.size = max_size
            self.recover_address = results[0]
            log.debug("Node: {} has the newer version of key: {}. Recovering it on node: {}"
                      .format(self.recover_address, repr(self.id), self.address))
            self.recover()

    def recover(self):
        self.recover_result = Recovery(key=self.id,
                                       timestamp=self.timestamp,
                                       size=self.size,
                                       address=self.recover_address,
                                       group=self.group,
                                       ctx=self.ctx,
                                       node=self.node,
                                       check=False,
                                       callback=self.onrecover)
        self.recover_result.run()

    def onrecover(self, result, stats):
        self.result &= result
        self.stats += stats;
        self.remove()

    def remove(self):
        addresses = [r.address for r in self.lookup_results if r and r.address not in [self.address, self.recover_address]]
        if addresses and not self.ctx.safe:
            log.debug("Removing key: {} from nodes: {}".format(repr(self.id), addresses))
            for addr in addresses:
                self.async_removes.append(RemoveDirect(addr, self.id, self.group, self.ctx, self.node, self.onremove))
                self.async_removes[-1].run()

    def wait(self):
        log.debug("Waiting lookup for key: {}".format(repr(self.id)))
        while hasattr(self, 'async_lookups') and self.async_lookups is not None:
            for r in self.async_lookups:
                try:
                    self.r.wait()
                except:
                    pass
        log.debug("Lookup completed for key: {}".format(repr(self.id)))
        if hasattr(self, 'recover_result'):
            self.recover_result.wait()

        log.debug("Waiting remove for key: {0}".format(repr(self.id)))
        if hasattr(self, 'async_removes') and self.async_removes is not None:
            for r in self.async_removes:
                try:
                    r.wait()
                except:
                    pass
        log.debug("Remove completed for key: {0}".format(repr(self.id)))

    def onremove(self, removed, stats):
        self.result &= removed
        self.stats += stats

    def succeeded(self):
        self.wait()
        return self.result


def dump_process_group(group):
    log.debug("Processing group: {}".format(group))
    ctx = g_ctx
    routes = ctx.routes.filter_by_group_id(group)
    stats = ctx.monitor.stats['group_{}'.format(group)]
    if not routes:
        log.error("Group: {} is not presented in route list".format(group))
        return False
    ctx.elog = elliptics.Logger(ctx.log_file, int(ctx.log_level))
    node = elliptics_create_node(address=ctx.address,
                                 elog=ctx.elog,
                                 wait_timeout=ctx.wait_timeout,
                                 net_thread_num=1,
                                 io_thread_num=1,
                                 remotes=ctx.remotes)
    ret = True
    with open(ctx.dump_file, 'r') as dump:
        for batch_id, batch in groupby(enumerate(dump), key=lambda x: x[0] / ctx.batch_size):
            recovers = []
            rs = RecoverStat()
            for _, val in batch:
                rec = DumpRecover(routes=routes, node=node, id=elliptics.Id(val), group=group, ctx=ctx)
                recovers.append(rec)
                rec.run()
            for r in recovers:
                r.wait()
                ret &= r.succeeded()
                rs += r.stats
            rs.apply(stats)
    return ret


def dump_main(ctx):
    global g_ctx
    g_ctx = ctx
    ctx.monitor.stats.timer('main', 'started')
    processes = min(g_ctx.nprocess, len(g_ctx.groups))
    log.info("Creating pool of processes: {0}".format(processes))
    pool = Pool(processes=processes, initializer=worker_init)
    ret = True

    try:
        results = pool.map(dump_process_group, ctx.groups)
    except KeyboardInterrupt:
        log.error("Caught Ctrl+C. Terminating.")
        pool.terminate()
        pool.join()
        ctx.monitor.stats.timer('main', 'finished')
        return False

    ret = all(results)

    log.info("Closing pool, joining threads.")
    pool.close()
    pool.join()
    ctx.monitor.stats.timer('main', 'finished')
    return ret
