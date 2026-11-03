/* Optional AV1 admission helper. Build against pinned dav1d, not private ABI.
 * stdin: TEDDAV1\1, big-endian item count, then (length,width,height,OBUs).
 * Version 2: magic ending in 2, stream count, then sample count and four av1C bytes per stream
 * and (length,width,height,sync,OBUs) per sample. One decoder owns each stream.
 * stdout on complete success only: input magic, picture count, decoded pixels.
 * No paths, identifiers, metadata, pixels, or decoder log text are emitted.
 * The launching image worker owns the shared process-tree deadline/memory cap.
 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif
#include "dav1d/dav1d.h"

#define BYTE_LIMIT (64U * 1024U * 1024U)
#define PIXEL_LIMIT 256000000U
/* Both qualified builds use b546257. Git-exported Linux input has no .git;
 * its generated upstream version is the release spelling, not the Git suffix.
 * Each resulting executable is separately hash-pinned by the Python adapter. */
#ifdef _WIN32
#define TEDD_DAV1D_VERSION "1.5.3-0-gb546257"
#else
#define TEDD_DAV1D_VERSION "1.5.3"
#endif
static const unsigned char magic[8] = {'T','E','D','D','A','V','1',1};

static int read_number(uint32_t *value) {
    unsigned char raw[4];
    if (fread(raw, 1, 4, stdin) != 4) return -1;
    *value = ((uint32_t)raw[0] << 24) | ((uint32_t)raw[1] << 16)
           | ((uint32_t)raw[2] << 8) | raw[3];
    return 0;
}

static int write_number(uint32_t value) {
    const unsigned char raw[4] = {
        (unsigned char)(value >> 24), (unsigned char)(value >> 16),
        (unsigned char)(value >> 8), (unsigned char)value
    };
    return fwrite(raw, 1, 4, stdout) == 4 ? 0 : -1;
}

static void refuse_log(void *cookie, const char *format, va_list args) {
    (void)format;
    (void)args;
    *(int *)cookie = 1;
}

static int accept_picture(Dav1dPicture *picture, unsigned *frames,
                          uint32_t width, uint32_t height, int sync,
                          const unsigned char *config) {
    int valid = !*frames && picture->p.w == (int)width
                && picture->p.h == (int)height;
    if (sync >= 0) {
        /* Finite sequence profile: one visible, single-layer picture/sample.
         * Hidden intermediate pictures and show-existing samples need their
         * own inventory/coverage contract; never ignore them on this route. */
        const Dav1dFrameHeader *frame = picture->frame_hdr;
        const Dav1dSequenceHeader *sequence = picture->seq_hdr;
        valid = valid && frame && frame->show_frame && !frame->show_existing_frame
                && !frame->temporal_id && !frame->spatial_id
                && (frame->frame_type == DAV1D_FRAME_TYPE_KEY) == sync
                && sequence && sequence->num_operating_points == 1
                && !sequence->operating_points[0].idc;
        if (valid) {
            /* Compare the container's mandatory configuration with the public
             * decoded header, not a second implementation of AV1 parsing. */
            /* Pinned dav1d 1.5.3 adds the major-level bias only for full
             * sequence headers; reduced still headers expose the raw index. */
            const unsigned bias = sequence->reduced_still_picture_header ? 0U : 2U;
            const unsigned level = (sequence->operating_points[0].major_level - bias) * 4U
                                   + sequence->operating_points[0].minor_level;
            const unsigned packed = (sequence->operating_points[0].tier << 7)
                                    | (!!sequence->hbd << 6) | ((sequence->hbd == 2) << 5)
                                    | (sequence->monochrome << 4) | (sequence->ss_hor << 3)
                                    | (sequence->ss_ver << 2) | sequence->chr;
            valid = config && config[0] == 0x81 && !config[3]
                    && config[1] == ((sequence->profile << 5) | level)
                    && config[2] == packed;
        }
    }
    ++*frames;
    dav1d_picture_unref(picture);
    return valid ? 0 : -1;
}

static int validate_sample(Dav1dContext *decoder, int *logged, uint32_t size,
                           uint32_t width, uint32_t height, int sync,
                           const unsigned char *config) {
    Dav1dData data = {0};
    Dav1dPicture picture = {0};
    unsigned frames = 0, steps = 0;
    int accepted = -1, result;
    uint8_t *input = dav1d_data_create(&data, size);
    if (!input || fread(input, 1, size, stdin) != size) goto cleanup;
    do {
        if (++steps > 512U) goto cleanup;
        result = dav1d_send_data(decoder, &data);
        if (result < 0 && result != DAV1D_ERR(EAGAIN)) goto cleanup;
        result = dav1d_get_picture(decoder, &picture);
        if (result == 0) {
            if (accept_picture(&picture, &frames, width, height, sync, config)) goto cleanup;
        } else if (result != DAV1D_ERR(EAGAIN)) goto cleanup;
        if (*logged) goto cleanup;
    } while (data.sz);
    for (;;) {
        if (++steps > 512U) goto cleanup;
        result = dav1d_get_picture(decoder, &picture);
        if (result == DAV1D_ERR(EAGAIN)) break;
        if (result < 0 || accept_picture(&picture, &frames, width, height, sync, config)) goto cleanup;
    }
    if (frames == 1 && !*logged) accepted = 0;
cleanup:
    dav1d_picture_unref(&picture);
    dav1d_data_unref(&data);
    return accepted;
}

static int validate_stream(unsigned version, uint32_t *bytes, uint32_t *pixels,
                           uint32_t *pictures) {
    Dav1dContext *decoder = NULL;
    Dav1dSettings settings;
    uint32_t count = 1;
    unsigned char config[4] = {0};
    int logged = 0, accepted = -1;
    if (version == 2 && read_number(&count)) return -1;
    if (!count || count > 256U - *pictures) return -1;
    if (version == 2 && (fread(config, 1, 4, stdin) != 4
        || config[0] != 0x81 || config[3])) return -1;
    dav1d_default_settings(&settings);
    settings.n_threads = 1;
    settings.max_frame_delay = 1;
    settings.frame_size_limit = 128000000U;
    settings.strict_std_compliance = 1;
    settings.output_invisible_frames = 1;
    settings.all_layers = 1;
    settings.logger.cookie = &logged;
    settings.logger.callback = refuse_log;
    if (dav1d_open(&decoder, &settings) < 0) goto cleanup;
    for (uint32_t index = 0; index < count; ++index) {
        uint32_t size, width, height, sync = 0;
        if (read_number(&size) || read_number(&width) || read_number(&height)
            || (version == 2 && read_number(&sync)) || sync > 1
            || (version == 2 && !index && !sync)
            || !size || size > BYTE_LIMIT - *bytes
            || !width || !height || width > 131072 || height > 131072
            || (uint64_t)width * height > 128000000U
            || (uint64_t)width * height > PIXEL_LIMIT - *pixels) goto cleanup;
        *bytes += size;
        *pixels += width * height;
        ++*pictures;
        if (validate_sample(decoder, &logged, size, width, height,
                            version == 2 ? (int)sync : -1, config)) goto cleanup;
    }
    accepted = 0;
cleanup:
    if (decoder) dav1d_close(&decoder);
    return accepted;
}

int main(int argc, char **argv) {
    unsigned char header[8];
    uint32_t count, bytes = 0, pixels = 0, pictures = 0;
    (void)argv;
    if (argc != 1 || strcmp(dav1d_version(), TEDD_DAV1D_VERSION)) return 1;
#ifdef _WIN32
    if (_setmode(_fileno(stdin), _O_BINARY) < 0
        || _setmode(_fileno(stdout), _O_BINARY) < 0) return 1;
#endif
    if (fread(header, 1, sizeof(header), stdin) != sizeof(header)
        || memcmp(header, magic, sizeof(magic) - 1)
        || (header[7] != 1 && header[7] != 2) || read_number(&count)
        || !count || count > 256) return 1;
    for (uint32_t index = 0; index < count; ++index) {
        if (validate_stream(header[7], &bytes, &pixels, &pictures)) return 1;
    }
    if (fgetc(stdin) != EOF || ferror(stdin)) return 1;
    if (fwrite(header, 1, sizeof(header), stdout) != sizeof(header)
        || write_number(pictures) || write_number(pixels) || fflush(stdout)) return 1;
    return 0;
}
