#include "route.h"
#include "elliptics.h"
#include <react/elliptics_react.hpp>
#include <elliptics/utils.hpp>

class dnet_pthread_mutex
{
public:
	dnet_pthread_mutex(pthread_mutex_t *mutex) : m_mutex(mutex)
	{
	}

	void lock()
	{
		pthread_mutex_lock(m_mutex);
	}

	void unlock()
	{
		pthread_mutex_unlock(m_mutex);
	}
private:
	pthread_mutex_t *m_mutex;
};

static int dnet_cmd_reverse_lookup(struct dnet_net_state *st, struct dnet_cmd *cmd, void *data __unused)
{
	struct dnet_node *n = st->n;
	int err = -ENXIO;
	int version[4] = {0, 0, 0, 0};
	int indexes_shard_count = 0;

	dnet_version_decode(&cmd->id, version);
	dnet_indexes_shard_count_decode(&cmd->id, &indexes_shard_count);
	memcpy(st->version, version, sizeof(st->version));

	dnet_version_encode(&cmd->id);
	dnet_indexes_shard_count_encode(&cmd->id, n->indexes_shard_count);

	err = dnet_version_check(st, version);
	if (err)
		goto err_out_exit;

	dnet_log(n, DNET_LOG_INFO, "%s: reverse lookup command: client indexes shard count: %d, server indexes shard count: %d\n",
			dnet_state_dump_addr(st),
			indexes_shard_count,
			n->indexes_shard_count);

	cmd->id.group_id = n->id.group_id;
	{
		pthread_mutex_lock(&n->state_lock);
		err = dnet_route_list_send_all_ids_nolock(st, &cmd->id, cmd->trans, DNET_CMD_REVERSE_LOOKUP, 1, 0);
		pthread_mutex_unlock(&n->state_lock);
	}

err_out_exit:
	if (err) {
		cmd->flags |= DNET_FLAGS_NEED_ACK;
		dnet_state_reset(st, err);
	}
	return err;
}

static int dnet_cmd_join_client(struct dnet_net_state *st, struct dnet_cmd *cmd, void *data)
{
	struct dnet_node *n = st->n;
	struct dnet_addr_container *cnt = (dnet_addr_container *)data;
	struct dnet_addr laddr;
	char client_addr[128], server_addr[128];
	int i, err, idx;
	uint32_t j;
	struct dnet_id_container *id_container;
	struct dnet_backend_ids **backends;
	struct dnet_backend_ids *backend;

	dnet_socket_local_addr(st->read_s, &laddr);
	idx = dnet_local_addr_index(n, &laddr);

	dnet_server_convert_dnet_addr_raw(&st->addr, client_addr, sizeof(client_addr));
	dnet_server_convert_dnet_addr_raw(&laddr, server_addr, sizeof(server_addr));

	if (cmd->size < sizeof(struct dnet_addr_container)) {
		dnet_log(n, DNET_LOG_ERROR, "%s: invalid join request: client: %s -> %s, "
				"cmd-size: %llu, must be more than addr_container: %zd\n",
				dnet_dump_id(&cmd->id), client_addr, server_addr,
				(unsigned long long)cmd->size, sizeof(struct dnet_addr_container));
		err = -EINVAL;
		goto err_out_exit;
	}

	dnet_convert_addr_container(cnt);

	if (cmd->size < sizeof(struct dnet_addr_container) + cnt->addr_num * sizeof(struct dnet_addr) + sizeof(struct dnet_id_container *)) {
		dnet_log(n, DNET_LOG_ERROR, "%s: invalid join request: client: %s -> %s, "
				"cmd-size: %llu, must be more than addr_container+addrs: %zd, addr_num: %d\n",
				dnet_dump_id(&cmd->id), client_addr, server_addr,
				(unsigned long long)cmd->size, sizeof(struct dnet_addr_container) + cnt->addr_num * sizeof(struct dnet_addr) + sizeof(struct dnet_id_container *),
				cnt->addr_num);
		err = -EINVAL;
		goto err_out_exit;
	}

	if (idx < 0 || idx >= cnt->addr_num || cnt->addr_num != n->addr_num) {
		dnet_log(n, DNET_LOG_ERROR, "%s: invalid join request: client: %s -> %s, "
				"address idx: %d, received addr-num: %d, local addr-num: %d\n",
				dnet_dump_id(&cmd->id), client_addr, server_addr,
				idx, cnt->addr_num, n->addr_num);
		err = -EINVAL;
		goto err_out_free;
	}

	id_container = (struct dnet_id_container *)((char *)data + sizeof(struct dnet_addr_container) + cnt->addr_num * sizeof(struct dnet_addr));

	backends = (struct dnet_backend_ids **)malloc(id_container->backends_count * sizeof(struct dnet_backend_ids *));
	if (!backends) {
		err = -ENOMEM;
		goto err_out_exit;
	}

	err = dnet_validate_id_container(id_container, cmd->size - sizeof(struct dnet_addr) * cnt->addr_num - sizeof(struct dnet_addr_container), backends);
	if (err) {
		dnet_log(n, DNET_LOG_ERROR, "%s: invalid join request: client: %s -> %s, failed to parse id_container, err: %d\n",
				dnet_dump_id(&cmd->id), client_addr, server_addr, err);
		goto err_out_free;
	}

	dnet_log(n, DNET_LOG_NOTICE, "%s: join request: client: %s -> %s, "
			"address idx: %d, received addr-num: %d, local addr-num: %d, backends-num: %d\n",
			dnet_dump_id(&cmd->id), client_addr, server_addr,
			idx, cnt->addr_num, n->addr_num, id_container->backends_count);

	for (i = 0; i < id_container->backends_count; ++i) {
		backend = backends[i];
		for (j = 0; j < backend->ids_count; ++j) {
			dnet_log(n, DNET_LOG_NOTICE, "%s: join request: client: %s -> %s, "
				"received backends: %d/%d, ids: %d/%d, addr-num: %d, idx: %d, backend_id: %d, group_id: %d, id: %s.\n",
				dnet_dump_id(&cmd->id), client_addr, server_addr,
				i, id_container->backends_count,
				j, backend->ids_count, cnt->addr_num, idx,
				backend->backend_id, backend->group_id,
				dnet_dump_id_str(backend->ids[i].id));
		}
	}

	list_del_init(&st->node_entry);
	list_del_init(&st->storage_state_entry);

	memcpy(&st->addr, &cnt->addrs[idx], sizeof(struct dnet_addr));

	err = dnet_copy_addrs(st, cnt->addrs, cnt->addr_num);
	if (err)
		goto err_out_free;

	for (i = 0; i < id_container->backends_count; ++i) {
		err = dnet_idc_update(st, backends[i]);
		if (err) {
			pthread_mutex_lock(&n->state_lock);
			dnet_idc_destroy_nolock(st);
			pthread_mutex_unlock(&n->state_lock);
			goto err_out_free;
		}
	}

	dnet_log(n, DNET_LOG_INFO, "%s: join request completed: client: %s -> %s, "
			"address idx: %d, received addr-num: %d, local addr-num: %d, backends-num: %d, err: %d\n",
			dnet_dump_id(&cmd->id), client_addr, server_addr,
			idx, cnt->addr_num, n->addr_num, id_container->backends_count, err);
err_out_free:
	free(backends);
err_out_exit:
	return err;
}

static int dnet_state_join_nolock(struct dnet_net_state *st)
{
	int err;
	struct dnet_node *n = st->n;
	struct dnet_id id;

	/* we do not care about group_id actually, since use direct send */
	memcpy(&id, &n->id, sizeof(id));

	err = dnet_route_list_send_all_ids_nolock(st, &id, 0, DNET_CMD_JOIN, 0, 1);
	if (err) {
		dnet_log(n, DNET_LOG_ERROR, "%s: failed to send join request to %s.\n",
			dnet_dump_id(&id), dnet_server_convert_dnet_addr(&st->addr));
		goto err_out_exit;
	}

	st->__join_state = DNET_JOIN;
	dnet_log(n, DNET_LOG_INFO, "%s: successfully joined network, group %d.\n", dnet_dump_id(&id), id.group_id);

err_out_exit:
	return err;
}

dnet_route_list::dnet_route_list(dnet_node *node) : m_node(node)
{
}

dnet_route_list::~dnet_route_list()
{
}

int dnet_route_list::enable_backend(size_t backend_id, int group_id, dnet_raw_id *ids, size_t ids_count)
{
	dnet_pthread_mutex mutex(&m_node->state_lock);
	std::lock_guard<dnet_pthread_mutex> guard(mutex);

	m_backends.resize(std::max(m_backends.size(), backend_id + 1));

	backend_info &backend = m_backends[backend_id];
	backend.activated = true;
	backend.group_id = group_id;
	backend.ids.assign(ids, ids + ids_count);

	return 0;
}

int dnet_route_list::disable_backend(size_t backend_id)
{
	dnet_pthread_mutex mutex(&m_node->state_lock);
	std::lock_guard<dnet_pthread_mutex> guard(mutex);

	if (backend_id >= m_backends.size()) {
		return 0;
	}

	backend_info &backend = m_backends[backend_id];
	backend.activated = false;

	return 0;
}

int dnet_route_list::on_reverse_lookup(dnet_net_state *st, dnet_cmd *cmd, void *data)
{
	react::action_guard action_guard(ACTION_DNET_CMD_REVERSE_LOOKUP);
	return dnet_cmd_reverse_lookup(st, cmd, data);
}

int dnet_route_list::on_join(dnet_net_state *st, dnet_cmd *cmd, void *data)
{
	react::action_guard action_guard(ACTION_DNET_CMD_JOIN_CLIENT);
	return dnet_cmd_join_client(st, cmd, data);
}

int dnet_route_list::join(dnet_net_state *st)
{
	dnet_pthread_mutex mutex(&st->n->state_lock);
	std::lock_guard<dnet_pthread_mutex> guard(mutex);

	return dnet_state_join_nolock(st);
}

struct free_destroyer
{
	void operator() (void *buffer)
	{
		free(buffer);
	}
};

int dnet_route_list::send_all_ids_nolock(dnet_net_state *st, dnet_id *id, uint64_t trans, unsigned int command, int reply, int direct)
{
	using namespace ioremap::elliptics;

	size_t total_size = sizeof(dnet_addr_cmd) + m_node->addr_num * sizeof(dnet_addr) + sizeof(dnet_id_container);

	for (auto it = m_backends.begin(); it != m_backends.end(); ++it) {
		total_size += sizeof(dnet_backend_ids);
		total_size += it->ids.size() * sizeof(dnet_raw_id);
	}

	void *buffer = std::malloc(total_size);
	if (!buffer)
		return -ENOMEM;
	std::unique_ptr<void, free_destroyer> buffer_guard(buffer);
	memset(buffer, 0, total_size);

	dnet_cmd *cmd = reinterpret_cast<dnet_cmd *>(buffer);
	cmd->id = *id;
	cmd->trans = trans;
	cmd->cmd = command;
	cmd->flags = DNET_FLAGS_NOLOCK;
	if (direct)
		cmd->flags |= DNET_FLAGS_DIRECT;
	if (reply)
		cmd->trans |= DNET_TRANS_REPLY;
	cmd->size = total_size - sizeof(dnet_cmd);

	dnet_addr_container *addr_container = reinterpret_cast<dnet_addr_container *>(cmd + 1);
	addr_container->addr_num = addr_container->node_addr_num = m_node->addr_num;

	dnet_addr *addrs = addr_container->addrs;
	memcpy(addrs, m_node->addrs, m_node->addr_num * sizeof(dnet_addr));

	dnet_id_container *id_container = reinterpret_cast<dnet_id_container *>(addrs + m_node->addr_num);
	id_container->backends_count = m_backends.size();

	dnet_backend_ids *backend_ids = reinterpret_cast<dnet_backend_ids *>(id_container + 1);

	for (size_t backend_id = 0; backend_id < m_backends.size(); ++backend_id) {
		backend_info &backend = m_backends[backend_id];
		backend_ids->backend_id = backend_id;
		backend_ids->group_id = backend.group_id;
		backend_ids->ids_count = backend.ids.size();

		dnet_raw_id *ids = backend_ids->ids;
		memcpy(ids, backend.ids.data(), backend.ids.size() * sizeof(dnet_raw_id));

		backend_ids = reinterpret_cast<dnet_backend_ids *>(ids + backend.ids.size());
	}

	return dnet_send(st, buffer, total_size);
}

dnet_route_list *dnet_route_list_create(dnet_node *node)
{
	return new dnet_route_list(node);
}

void dnet_route_list_destroy(dnet_route_list *route)
{
	delete route;
}

template <typename Method, typename... Args>
static int safe_call(dnet_route_list *route, Method method, Args &&...args)
{
	try {
		return (route->*method)(std::forward<Args>(args)...);
	} catch (std::bad_alloc &) {
		return -ENOMEM;
	} catch (...) {
		return -EINVAL;
	}
}

int dnet_route_list_reverse_lookup(dnet_net_state *st, dnet_cmd *cmd, void *data)
{
	return safe_call(st->n->route, &dnet_route_list::on_reverse_lookup, st, cmd, data);
}

int dnet_route_list_join(dnet_net_state *st, dnet_cmd *cmd, void *data)
{
	return safe_call(st->n->route, &dnet_route_list::on_join, st, cmd, data);
}

int dnet_state_join(struct dnet_net_state *st)
{
	return safe_call(st->n->route, &dnet_route_list::join, st);
}

int dnet_route_list_enable_backend(dnet_route_list *route, size_t backend_id, int group_id, dnet_raw_id *ids, size_t ids_count)
{
	return safe_call(route, &dnet_route_list::enable_backend, backend_id, group_id, ids, ids_count);
}

int dnet_route_list_disable_backend(dnet_route_list *route, size_t backend_id)
{
	return safe_call(route, &dnet_route_list::disable_backend, backend_id);
}

int dnet_route_list_send_all_ids_nolock(dnet_net_state *st, dnet_id *id, uint64_t trans, unsigned int command, int reply, int direct)
{
	return safe_call(st->n->route, &dnet_route_list::send_all_ids_nolock, st, id, trans, command, reply, direct);
}
