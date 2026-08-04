# FlexURL Performance Optimization Audit Report

This report outlines the architecture and optimizations implemented to scale the redirect endpoint, maximize throughput, and ensure production readiness.

---

## 1. Architecture Overview

### System Architecture Diagram
```mermaid
graph TD
    Client[Client Request] -->|GET /{short_url}| App[FastAPI Router]
    App -->|GET / SET| Redis[(Redis Cache)]
    App -->|Enqueue Job| Queue[ARQ Job Queue]
    App -->|Fallback GET| DB[(PostgreSQL)]
    
    Queue -->|Process Event| Worker[ARQ Background Worker]
    Worker -->|Geo-IP Lookup| GeoAPI[ip-api.com]
    Worker -->|Write Clicklog| DB
    Worker -->|Increment clicks:{url}| Redis
    Worker -->|POST Webhook| Webhook[Outbound Webhooks]
    
    Cron[ARQ Cron Trigger] -->|Every 30s| Lock{Redis Distributed Lock}
    Lock -->|Acquired| FlushTask[Flush Clicks Task]
    FlushTask -->|Bulk UPDATE click_count| DB
    FlushTask -->|Atomic DECRBY| Redis
```

---

## 2. Workflows & Flows

### Redirect Path Flow
1. Client requests `GET /{short_url}`.
2. The router extracts the `host` header to identify any custom domain context.
3. The router queries the Redis cache for `dom_owner:{host}` (if custom domain) and `short_url`.
4. **Cache Hit**:
   - If the cached value is a standard destination URL, it enqueues the analytics event and immediately redirects with `302`.
   - If the cached value is a JSON configuration dictionary (dynamic URL), the router processes the expiration, scheduled activation, password gate, and OS targeting logic in-memory, enqueues the event, and redirects.
5. **Cache Miss**:
   - The router queries PostgreSQL for the short URL record.
   - If dynamic, the router queries the user subscription tier, packages the dynamic configuration fields into a compact JSON payload, writes it to Redis, enqueues the event, and redirects.
   - If static, the router writes the raw destination URL string to Redis, enqueues the event, and redirects.

### Analytics Queue Flow
1. Redirect route handler captures immediately available HTTP metadata (IP, UA, Referer, short URL, timestamp).
2. It pushes the lightweight payload asynchronously to Redis using `arq_pool.enqueue_job`.
3. The ARQ worker process consumes the job:
   - Queries `geo:{ip}` in Redis (falls back to `ip-api.com` HTTP call on cache miss).
   - Parses the User-Agent and Referer strings in-memory.
   - Saves a visitor metric entry to the PostgreSQL `clicklog` table.
   - Atomically increments `clicks:{short_url}` in Redis.
   - Dispatches webhook POST requests (if premium owner has configured webhooks).

---

## 3. Storage Engine Usage

### Redis Usage
- `{short_url}`: Stores raw destination URLs (for static links) or compact JSON configurations (for dynamic links).
- `dom_owner:{host}`: Caches the `user_id` owner of custom domains.
- `user_tier:{user_id}`: Caches subscription tier strings.
- `geo:{ip}`: Caches country and city string combinations (`Country|City`) for 7 days to eliminate Geo-IP lookup latency.
- `clicks:{short_url}`: Buffers click counts to protect PostgreSQL from concurrent write locks.
- `lock:flush_clicks`: Distributed lock key used to orchestrate click flushes across multiple workers.

### PostgreSQL Usage
- `urldata`: Holds short-to-long mapping rows. The click count is updated periodically using atomic batch updates.
- `clicklog`: Standard logging table holding visitor metadata.
- `users` / `subscriptions` / `custom_domains`: Holds administrative settings and custom properties.

---

## 4. Cache Invalidation Table

Whenever any configuration change occurs, the corresponding Redis key is invalidated:

| Operation | Invalidation Strategy | Redis Key |
| :--- | :--- | :--- |
| **URL Edit / Update** | Unconditional delete | `{short_url}` |
| **Link Deletion** | Unconditional delete | `{short_url}` |
| **Safe Browsing Ban** | Overwrite with `"BANNED"` | `{short_url}` |
| **Manual Unban** | Unconditional delete | `{short_url}` |
| **Custom Domain Register** | Unconditional delete | `dom_owner:{host}` |
| **Custom Domain Delete** | Unconditional delete | `dom_owner:{host}` |
| **User Subscription Change** | Unconditional delete | `user_tier:{user_id}` |

---

## 5. Failure & Reliability Management

- **Redis Server Offline**:
  - The redirect handler catches exceptions on Redis lookup/write and falls back to database lookup. Redirections continue to work.
  - Analytics enqueueing fails silently with a warning log, and redirects continue.
- **PostgreSQL Offline**:
  - Redirections hitting active Redis cache entries continue to serve successfully. Cache misses fail with a 503 error.
  - Background worker database writes fail. Exception is caught, logged, and the Redis click counters are **not** decremented. Analytics are retried on subsequent flush cycles.
- **ARQ Worker Offline / Crashes**:
  - Task enqueuing continues to place jobs in Redis. When the worker recovers, it processes the backlog.
  - Click counts remain buffered in Redis and are not lost.
- **Geo-IP Timeout / API Failure**:
  - A timeout (2.0s) or status error is caught. The country and city default to `"Unknown"`, and click logging and counter increments proceed without delay.
- **Webhook Timeout / Delivery Failure**:
  - Webhook POST requests are limited to a 5.0s timeout. If delivery fails or times out, the exception is caught, logged as a warning, and the worker completes the job.

---

## 6. Distributed Locking Pattern

To prevent double-counting of clicks when running multiple workers:
- **Lock Acquisition**: `flush_clicks` executes `redis_client.set(lock_key, token, nx=True, ex=60)`. If another worker owns the lock, it exits immediately.
- **Unique Tokens**: Every execution generates a unique UUID `token`.
- **Atomic Lock Release**: The lock is released in a `finally` block using a Lua script to ensure a worker never deletes another worker's lock if the task runs longer than the lock timeout:
  ```lua
  if redis.call("get", KEYS[1]) == ARGV[1] then
      return redis.call("del", KEYS[1])
  else
      return 0
  end
  ```

---

## 7. Performance Optimizations Checklist

- [x] Analytics offloaded
- [x] Queue implemented
- [x] Duplicate DB lookups removed
- [x] HTML cached
- [x] Global AsyncClient
- [x] Distributed lock
- [x] Cache invalidation audited
- [x] Production logging
- [x] Failure recovery tested
