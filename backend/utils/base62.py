ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"



def encode_base62(num) -> str:
    if num == 0:
        return "0"

    result = []

    while num:
        num, rem = divmod(num, 62)
        result.append(ALPHABET[rem])

    return ''.join(reversed(result))


