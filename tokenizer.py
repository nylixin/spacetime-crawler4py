import sys

# Runtime: O(n), where n = number of characters in file. It runs in linear time relative to the size of the input file. 
# Explaination: We read the file character by character, checking if the character is alphanumeric. We only pass through the characters once, making this
# O(n), where n is the number of characters in the file. All other operations are O(1). 
def tokenize_text(text: str) -> list:
    tokens = []
    token = []
    for ch in text:
        if ch.isalnum():
            token.append(ch.lower())
        else:
            if token:
                tokens.append("".join(token))
                token = []
    if token:
        tokens.append("".join(token))
    return tokens

# Runtime: O(n), where n = number of tokens. It runs in linear time relative to the number of tokens in the list.
# Explaination: We iterate through the list of tokens once while updating the frequency of appearance in a dictionary.
# Updates are O(1) but the loop makes this O(n) where n is the number of tokens in the list.
def compute_word_frequencies(tokens: list) -> dict:
    frequencies = {}
    for token in tokens:
        frequencies[token] = frequencies.get(token, 0) + 1
    return frequencies

# Runtime: O(n log n), where n = number of unique tokens. It runs in log-linear time relative to the number of unique tokens in the frequency dictionary.
# Explaination: We first sort the dictionary items by frequency using the sorted() function, this function has a time
# complexity of O(n log n). After sorting, we print all sorted items which will take O(n) time, but since O(n log n) is greater, the 
# overall time is O(n log n).
def printFrequencies(frequencies: dict):
    sorted_items = sorted(frequencies.items(), key=lambda x: -x[1])

    for token, count in sorted_items:
        print(f"{token} -> {count}")