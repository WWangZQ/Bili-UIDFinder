# B站 UID 筛选工具

扫描 B站 UID，筛选**零级 + 纯英文昵称**的账号。

## 安装

```bash
pip install httpx
```

## 使用方式

### 按后缀扫描 (`main_api.py`)

扫描所有以指定 4 位数为后缀的 UID（前缀 1-999，共 999 个）。

```bash
# 交互模式
python main_api.py

# 命令行模式
python main_api.py --suffix 1314

# 使用代理池
python main_api.py --suffix 1314 --pool
```

输出文件：`uid_{suffix}.txt`

### 按回文扫描 (`main_palindrome.py`)

扫描所有指定位数的回文 UID。

```bash
python main_palindrome.py --digits 7
python main_palindrome.py --digits 7 --lo 5000000 --hi 6000000 --pool
```

输出文件：`uid_palindrome_{n}d.txt`

### 代理池

`--pool` 自动从 Geonode / PubProxy 获取免费代理，验证后轮换使用，遇到 412 自动换代理。也可手动指定：

```bash
python main_api.py --suffix 0622 --proxy http://host:port
```

## 输出格式

```
nickname uid        ← 满足条件的（零级 + 纯英文昵称）
...
                    ← 空行
 nickname uid       ← 全部结果（无昵称的前面为空格）
...
```

## 参数

| 参数 | 说明 |
|------|------|
| `--pool` | 使用免费代理池 |
| `--proxy URL` | 手动指定代理 |
| `--suffix` / `-s` | 四位数后缀（仅 `main_api.py`）|
| `--digits` / `-d` | 回文位数（仅 `main_palindrome.py`）|
| `--lo` / `--hi` | UID 范围（仅 `main_palindrome.py`）|
