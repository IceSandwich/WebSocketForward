# Server Configuration

## HAProxy

[Ref](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/load-balancing/websocket/)

```conf
frontend fe_main
  bind :80
  use_backend websocket_servers if { path_beg /wsf }
  default_backend http_servers

backend websocket_servers
  #option http-server-close
  #timeout client  1h  # 设置客户端连接的超时时间为 1 小时
  #timeout server  1h  # 设置服务器连接的超时时间为 1 小时
  timeout tunnel 1h
  server wsf 127.0.0.1:12228
```

## Nginx

[Ref](http://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_connect_timeout)

```conf
http {
    upstream websocket_backend {
        # 定义多个 WebSocket 后端服务器，轮询负载均衡
        server 127.0.0.1:8080;
        server 127.0.0.1:8081;
    }

    server {
        listen 80;

        location /ws {  # 监听 WebSocket 连接
            proxy_pass http://websocket_backend;  # 将请求转发到 upstream
            proxy_http_version 1.1;  # 强制使用 HTTP/1.1（WebSocket 需要）
            proxy_set_header Upgrade $http_upgrade;  # 设置 WebSocket 升级头
            proxy_set_header Connection 'upgrade';  # 设置 Connection 为 'upgrade'
            proxy_set_header Host $host;  # 保持原始主机头部
            proxy_cache_bypass $http_upgrade;  # 禁止缓存
            proxy_set_header X-Real-IP $remote_addr;  # 传递客户端 IP
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;  # 转发客户端真实 IP
            proxy_set_header X-Forwarded-Proto $scheme;  # 转发请求的协议（http/https）
            proxy_read_timeout 3600s;  # 设置长连接的超时时间
            proxy_send_timeout 3600s;  # 设置发送请求的超时时间

            # try the following.
            #proxy_connect_timeout 7d;
            #proxy_send_timeout 7d;
            #proxy_read_timeout 7d;
        }
    }
}
```