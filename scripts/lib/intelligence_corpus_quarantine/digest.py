"""The sole content-equality definition, preserving SQLite storage classes."""
import hashlib


def table_digest(conn, table, columns, key, where=None):
    """Stream ordered rows; key must be a unique signed SQLite integer.

    Identifiers are quoted; where is trusted caller-supplied SQL, not user input.
    Missing columns, invalid keys and unsupported storage classes raise.
    """
    if columns is None or isinstance(columns, (str, bytes)):
        raise ValueError("columns must be a sequence of identifiers")
    columns = list(columns)
    identifiers = [table, key, *columns]
    if not columns or any(not isinstance(n, str) or not n or "\0" in n for n in identifiers):
        raise ValueError("table, key and columns must be nonempty identifiers")
    quoted = ['"' + name.replace('"', '""') + '"' for name in identifiers]
    table_sql, key_sql, *column_sql = quoted
    selected = ", ".join(f"t.{name}, typeof(t.{name})" for name in [key_sql, *column_sql])
    sql = f"SELECT {selected} FROM {table_sql} AS t"
    if where is not None:
        if not isinstance(where, str) or not where.strip():
            raise ValueError("where must be nonempty SQL")
        sql += f" WHERE {where}"
    digest = hashlib.sha256()
    rows, row_min, row_max = 0, None, None
    for record in conn.execute(sql + f" ORDER BY t.{key_sql}"):
        value, storage = record[:2]
        if storage != "integer" or type(value) is not int or (rows and value <= row_max):
            raise ValueError("key must contain unique ascending integers")
        digest.update(b"R" + value.to_bytes(8, "big", signed=True))
        row_min, row_max = value if rows == 0 else row_min, value
        for value, storage in zip(record[2::2], record[3::2]):
            if storage == "null":
                payload = b"N"
            elif storage == "integer":
                payload = b"I" + str(value).encode("ascii") + b";"
            elif storage == "real":
                payload = b"F" + repr(float(value)).encode("ascii") + b";"
            elif storage in ("text", "blob"):
                data = value.encode("utf-8") if storage == "text" else bytes(value)
                payload = (b"T" if storage == "text" else b"B") + len(data).to_bytes(8, "big") + data
            else:
                raise ValueError(f"unsupported storage class: {storage}")
            digest.update(payload)
        digest.update(b"\n")
        rows += 1
    return dict(rows=rows, row_min=row_min, row_max=row_max, columns=columns,
                digest_sha256=digest.hexdigest())
