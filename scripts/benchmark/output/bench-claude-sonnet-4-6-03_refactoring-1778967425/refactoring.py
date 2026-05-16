def _is_item_active(item: dict) -> bool:
    return item.get('s') == 'active'


def _build_timestamp_error(item: dict, index: int) -> dict:
    return {'idx': index, 'msg': 'missing ts', 'id': item.get('id')}


def _is_within_cutoff(timestamp: float, cutoff_timestamp: float) -> bool:
    return timestamp <= cutoff_timestamp


def _apply_maximum_cap(item: dict, value: float | int, config: dict) -> float | int:
    maximum_value = config.get('max_v', 100)
    if value > maximum_value:
        item['capped'] = True
        return maximum_value
    return value


def _validate_minimum_value(
    item: dict, index: int, value: float | int, config: dict
) -> dict | None:
    minimum_value = config.get('min_v', 0)
    if value < minimum_value:
        return {'idx': index, 'msg': 'below min', 'id': item.get('id'), 'val': value}
    return None


def _apply_multiplier(value: float | int, config: dict) -> float | int:
    multiplier = config.get('mult', 1.0)
    return value * multiplier


def _apply_rounding(value: float | int, config: dict) -> float | int:
    if config.get('round'):
        decimal_places = config.get('round_dp', 2)
        return round(value, decimal_places)
    return value


def _build_result_entry(item: dict, value: float | int, timestamp: float) -> dict:
    return {
        'id': item.get('id'),
        'v': value,
        'ts': timestamp,
        'src': item.get('src', 'unknown'),
    }


def process_active_items(
    items: list[dict], cutoff_timestamp: float, config: dict
) -> tuple[list[dict], list[dict]]:
    results: list[dict] = []
    errors: list[dict] = []

    for index, item in enumerate(items):
        if not _is_item_active(item):
            continue

        timestamp = item.get('ts')
        if timestamp is None:
            errors.append(_build_timestamp_error(item, index))
            continue

        if not _is_within_cutoff(timestamp, cutoff_timestamp):
            continue

        value = item.get('v', 0)
        value = _apply_maximum_cap(item, value, config)

        minimum_error = _validate_minimum_value(item, index, value, config)
        if minimum_error is not None:
            errors.append(minimum_error)
            continue

        value = _apply_multiplier(value, config)
        value = _apply_rounding(value, config)

        results.append(_build_result_entry(item, value, timestamp))

    return results, errors
