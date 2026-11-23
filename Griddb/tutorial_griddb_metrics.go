package main

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand"
	"os"
	"runtime"
	"time"

	"example.com/util"
)

type Meta struct {
	Format      string     `json:"format"`
	Endianness  string     `json:"endianness"`
	DBRecords   uint64     `json:"db_records"`
	RecordWords uint64     `json:"record_words"`
	GridNumRows uint64     `json:"grid_num_rows"`
	XEdges      []float64  `json:"x_edges"`
	YEdges      []float64  `json:"y_edges"`
	XBlocks     [][]uint64 `json:"x_blocks"`
	YBlocks     [][]uint64 `json:"y_blocks"`
}

type QueryIndex struct {
	Query    []float64 `json:"query"`
	GridCol  uint64    `json:"grid_col"`
	GridRow  uint64    `json:"grid_row"`
	PIRIndex uint64    `json:"pir_index"`
}

type Vec []uint64

type LocalHint struct {
	key             util.PrfKey
	parity          Vec
	programmedPoint uint64
	isProgrammed    bool
}

type PianoState struct {
	primaryHints           []LocalHint
	backupHints            []LocalHint
	replacementIndices     []uint64
	replacementValues      []Vec
	consumedReplacementNum []uint64
	consumedHintNum        []uint64
	localCache             map[uint64]Vec
	M1                     uint64
	M2                     uint64
}

type PhaseTimes struct {
	LoadDB        time.Duration `json:"load_db_time"`
	Setup         time.Duration `json:"setup_time"`
	ClientFind    time.Duration `json:"client_find_time"`
	Server        time.Duration `json:"server_time"`
	ClientRecover time.Duration `json:"client_recover_time"`
	OnlineTotal   time.Duration `json:"online_total_time"`
	EndToEnd      time.Duration `json:"end_to_end_time"`
}

type CommMetrics struct {
	RecordBytes             uint64 `json:"record_bytes"`
	LogicalDBRecords        uint64 `json:"logical_db_records"`
	PhysicalDBRecords       uint64 `json:"physical_db_records"`
	DBFileBytes             uint64 `json:"db_file_bytes"`
	SetupDownloadBytes      uint64 `json:"setup_download_bytes"`
	OnlineRequestBytes      uint64 `json:"online_request_bytes"`
	OnlineResponseBytes     uint64 `json:"online_response_bytes"`
	OnlineTotalBytes        uint64 `json:"online_total_bytes"`
	ServerLocalReadBytes    uint64 `json:"server_local_read_bytes"`
	ClientHintStorageBytes  uint64 `json:"client_hint_storage_bytes_est"`
	ReplacementStorageBytes uint64 `json:"replacement_storage_bytes_est"`
}

type MemSnapshot struct {
	HeapAlloc uint64 `json:"heap_alloc"`
	Sys       uint64 `json:"sys"`
}

type BenchResult struct {
	DBRecords        uint64           `json:"db_records"`
	RecordWords      uint64           `json:"record_words"`
	ChunkSize        uint64           `json:"chunk_size"`
	ChunkNum         uint64           `json:"chunk_num"`
	M1               uint64           `json:"m1"`
	M2               uint64           `json:"m2"`
	PIRIndex         uint64           `json:"pir_index"`
	GridCol          uint64           `json:"grid_col"`
	GridRow          uint64           `json:"grid_row"`
	SkylineCount     int              `json:"skyline_count"`
	SkylineIDs       []uint64         `json:"skyline_ids,omitempty"`
	TimesMicro       map[string]int64 `json:"times_micro"`
	Communication    CommMetrics      `json:"communication"`
	MemoryBefore     MemSnapshot      `json:"memory_before_setup"`
	MemoryAfterSetup MemSnapshot      `json:"memory_after_setup"`
	MemoryAfterQuery MemSnapshot      `json:"memory_after_query"`
}

var FullDB []Vec
var DBSize uint64
var ChunkSize uint64
var ChunkNum uint64
var RecordWords uint64

func zeroVec() Vec { return make(Vec, RecordWords) }

func cloneVec(v Vec) Vec {
	out := make(Vec, len(v))
	copy(out, v)
	return out
}

func xorInto(dst Vec, src Vec) {
	if len(dst) != len(src) {
		panic("vector length mismatch")
	}
	for i := range dst {
		dst[i] ^= src[i]
	}
}

func xorNew(a Vec, b Vec) Vec {
	out := cloneVec(a)
	xorInto(out, b)
	return out
}

func equalVec(a Vec, b Vec) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func loadMeta(path string) (Meta, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Meta{}, err
	}
	var meta Meta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return Meta{}, err
	}
	if meta.RecordWords == 0 || meta.DBRecords == 0 {
		return Meta{}, errors.New("invalid metadata: record_words and db_records must be positive")
	}
	if meta.Endianness != "" && meta.Endianness != "little" {
		return Meta{}, fmt.Errorf("unsupported endianness: %s", meta.Endianness)
	}
	return meta, nil
}

func loadFullDB(path string, meta Meta) error {
	RecordWords = meta.RecordWords
	DBSize = meta.DBRecords
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	FullDB = make([]Vec, DBSize)
	buf := make([]byte, RecordWords*8)
	for i := uint64(0); i < DBSize; i++ {
		if _, err := io.ReadFull(f, buf); err != nil {
			return fmt.Errorf("short DB file at record %d: %w", i, err)
		}
		rec := make(Vec, RecordWords)
		for j := uint64(0); j < RecordWords; j++ {
			rec[j] = binary.LittleEndian.Uint64(buf[j*8 : (j+1)*8])
		}
		FullDB[i] = rec
	}
	return nil
}

func padDBForPiano() {
	ChunkSize = uint64(math.Ceil(math.Sqrt(float64(DBSize))))
	if ChunkSize == 0 {
		ChunkSize = 1
	}
	ChunkNum = uint64(math.Ceil(float64(DBSize) / float64(ChunkSize)))
	physicalSize := ChunkSize * ChunkNum
	for uint64(len(FullDB)) < physicalSize {
		FullDB = append(FullDB, zeroVec())
	}
}

func elem(hint *LocalHint, chunkId uint64) uint64 {
	if hint.isProgrammed && chunkId == hint.programmedPoint/ChunkSize {
		return hint.programmedPoint
	}
	return util.PRFEval(&hint.key, chunkId)%ChunkSize + chunkId*ChunkSize
}

func computeParams() (uint64, uint64) {
	l := math.Log(float64(DBSize + 2))
	if l < 1.0 {
		l = 1.0
	}
	sqrtN := math.Sqrt(float64(DBSize))
	m1 := uint64(math.Ceil(4 * sqrtN * l))
	m2 := uint64(math.Ceil(4 * l))
	if m1 < 64 {
		m1 = 64
	}
	if m2 < 8 {
		m2 = 8
	}
	return m1, m2
}

func setupPiano(rng *rand.Rand) (*PianoState, time.Duration) {
	start := time.Now()
	M1, M2 := computeParams()
	st := &PianoState{
		primaryHints:           make([]LocalHint, M1),
		backupHints:            make([]LocalHint, M2*ChunkNum),
		replacementIndices:     make([]uint64, M2*ChunkNum),
		replacementValues:      make([]Vec, M2*ChunkNum),
		consumedReplacementNum: make([]uint64, ChunkNum),
		consumedHintNum:        make([]uint64, ChunkNum),
		localCache:             make(map[uint64]Vec),
		M1:                     M1,
		M2:                     M2,
	}
	for i := uint64(0); i < M1; i++ {
		st.primaryHints[i] = LocalHint{key: util.RandKey(rng), parity: zeroVec()}
	}
	for i := uint64(0); i < M2*ChunkNum; i++ {
		st.backupHints[i] = LocalHint{key: util.RandKey(rng), parity: zeroVec()}
	}
	for i := uint64(0); i < M2*ChunkNum; i++ {
		st.replacementValues[i] = zeroVec()
	}

	for chunk := uint64(0); chunk < ChunkNum; chunk++ {
		for j := uint64(0); j < M1; j++ {
			idx := elem(&st.primaryHints[j], chunk)
			if idx < uint64(len(FullDB)) {
				xorInto(st.primaryHints[j].parity, FullDB[idx])
			}
		}
		for j := uint64(0); j < M2*ChunkNum; j++ {
			if j/M2 != chunk {
				idx := elem(&st.backupHints[j], chunk)
				if idx < uint64(len(FullDB)) {
					xorInto(st.backupHints[j].parity, FullDB[idx])
				}
			}
		}
		for j := chunk * M2; j < (chunk+1)*M2; j++ {
			idx := rng.Uint64()%ChunkSize + chunk*ChunkSize
			st.replacementIndices[j] = idx
			if idx < uint64(len(FullDB)) {
				st.replacementValues[j] = cloneVec(FullDB[idx])
			}
		}
	}
	return st, time.Since(start)
}

func queryPiano(st *PianoState, index uint64) (Vec, PhaseTimes, error) {
	var pt PhaseTimes

	totalStart := time.Now()
	chunkId := index / ChunkSize

	clientFindStart := time.Now()
	hitId := uint64(math.MaxUint64)
	for i := uint64(0); i < st.M1; i++ {
		if elem(&st.primaryHints[i], chunkId) == index {
			hitId = i
			break
		}
	}
	if hitId == uint64(math.MaxUint64) {
		return nil, pt, errors.New("cannot find a primary hint hitting the requested index; rerun with another seed or larger M1")
	}

	expandedSet := make([]uint64, ChunkNum)
	for i := uint64(0); i < ChunkNum; i++ {
		expandedSet[i] = elem(&st.primaryHints[hitId], i)
	}

	if st.consumedReplacementNum[chunkId] >= st.M2 {
		return nil, pt, errors.New("not enough replacement values for this chunk")
	}
	tmp := st.consumedReplacementNum[chunkId] + chunkId*st.M2
	replacementInd := st.replacementIndices[tmp]
	replacementVal := st.replacementValues[tmp]
	st.consumedReplacementNum[chunkId]++
	expandedSet[chunkId] = replacementInd
	pt.ClientFind = time.Since(clientFindStart)

	serverStart := time.Now()
	parity := zeroVec()
	for _, idx := range expandedSet {
		if idx < uint64(len(FullDB)) {
			xorInto(parity, FullDB[idx])
		}
	}
	pt.Server = time.Since(serverStart)

	recoverStart := time.Now()
	answer := xorNew(parity, st.primaryHints[hitId].parity)
	xorInto(answer, replacementVal)
	if !equalVec(answer, FullDB[index]) {
		return nil, pt, errors.New("PIR answer verification failed")
	}
	st.localCache[index] = cloneVec(answer)
	if st.consumedHintNum[chunkId] >= st.M2 {
		return nil, pt, errors.New("not enough backup hints for this chunk")
	}
	st.primaryHints[hitId] = st.backupHints[chunkId*st.M2+st.consumedHintNum[chunkId]]
	st.primaryHints[hitId].isProgrammed = true
	st.primaryHints[hitId].programmedPoint = index
	xorInto(st.primaryHints[hitId].parity, answer)
	st.consumedHintNum[chunkId]++
	pt.ClientRecover = time.Since(recoverStart)
	pt.OnlineTotal = pt.ClientFind + pt.Server + pt.ClientRecover
	pt.EndToEnd = time.Since(totalStart)
	return answer, pt, nil
}

func locateIndex(edges []float64, value float64) (uint64, error) {
	if len(edges) < 2 {
		return 0, errors.New("invalid edges")
	}
	if value < edges[0] || value > edges[len(edges)-1] {
		return 0, fmt.Errorf("value %f outside grid range [%f,%f]", value, edges[0], edges[len(edges)-1])
	}
	lo, hi := 0, len(edges)
	for lo < hi {
		mid := (lo + hi) / 2
		if edges[mid] >= value {
			hi = mid
		} else {
			lo = mid + 1
		}
	}
	i := lo - 1
	if i < 0 {
		i = 0
	}
	if i >= len(edges)-1 {
		i = len(edges) - 2
	}
	return uint64(i), nil
}

func locateBlock(blocks [][]uint64, fineIdx uint64) (uint64, error) {
	for i, b := range blocks {
		if len(b) != 2 {
			return 0, errors.New("invalid block format")
		}
		if b[0] <= fineIdx && fineIdx < b[1] {
			return uint64(i), nil
		}
	}
	return 0, fmt.Errorf("fine index %d outside blocks", fineIdx)
}

func indexFromQuery(meta Meta, qx float64, qy float64) (uint64, uint64, uint64, error) {
	fc, err := locateIndex(meta.XEdges, qx)
	if err != nil {
		return 0, 0, 0, err
	}
	fr, err := locateIndex(meta.YEdges, qy)
	if err != nil {
		return 0, 0, 0, err
	}
	gc, err := locateBlock(meta.XBlocks, fc)
	if err != nil {
		return 0, 0, 0, err
	}
	gr, err := locateBlock(meta.YBlocks, fr)
	if err != nil {
		return 0, 0, 0, err
	}
	return gc, gr, gc*meta.GridNumRows + gr, nil
}

func decodeRecord(record Vec) []uint64 {
	if len(record) == 0 {
		return nil
	}
	count := record[0]
	if count > uint64(len(record)-1) {
		count = uint64(len(record) - 1)
	}
	ids := make([]uint64, 0, count)
	for i := uint64(0); i < count; i++ {
		if record[1+i] != 0 {
			ids = append(ids, record[1+i])
		}
	}
	return ids
}

func memSnapshot() MemSnapshot {
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return MemSnapshot{HeapAlloc: m.HeapAlloc, Sys: m.Sys}
}

func estimateComm(st *PianoState) CommMetrics {
	recordBytes := RecordWords * 8
	physicalRecords := uint64(len(FullDB))
	keyBytes := uint64(16)                       // util.PrfKey is AES-128 key
	hintBytes := keyBytes + recordBytes + 16 + 1 // key + parity + programmedPoint/flag rough overhead, protocol-level estimate
	replacementBytes := 8 + recordBytes
	setupDownloadBytes := physicalRecords * recordBytes
	onlineReq := ChunkNum * 8
	onlineResp := recordBytes
	return CommMetrics{
		RecordBytes:             recordBytes,
		LogicalDBRecords:        DBSize,
		PhysicalDBRecords:       physicalRecords,
		DBFileBytes:             DBSize * recordBytes,
		SetupDownloadBytes:      setupDownloadBytes,
		OnlineRequestBytes:      onlineReq,
		OnlineResponseBytes:     onlineResp,
		OnlineTotalBytes:        onlineReq + onlineResp,
		ServerLocalReadBytes:    ChunkNum * recordBytes,
		ClientHintStorageBytes:  (st.M1 + st.M2*ChunkNum) * hintBytes,
		ReplacementStorageBytes: st.M2 * ChunkNum * replacementBytes,
	}
}

func micros(d time.Duration) int64 { return d.Microseconds() }
func mb(x uint64) float64          { return float64(x) / 1024.0 / 1024.0 }

func printSummary(br BenchResult) {
	c := br.Communication
	fmt.Println("\n================ Piano-PIR GRID Benchmark ================")
	fmt.Printf("DB records=%d, record_words=%d, record_bytes=%d\n", br.DBRecords, br.RecordWords, c.RecordBytes)
	fmt.Printf("ChunkSize=%d, ChunkNum=%d, M1=%d, M2=%d\n", br.ChunkSize, br.ChunkNum, br.M1, br.M2)
	fmt.Printf("PIR index=%d, grid=(%d,%d), skyline_count=%d\n", br.PIRIndex, br.GridCol, br.GridRow, br.SkylineCount)
	fmt.Println("\nTime (microseconds):")
	fmt.Printf("  load_db         = %d\n", br.TimesMicro["load_db"])
	fmt.Printf("  setup           = %d\n", br.TimesMicro["setup"])
	fmt.Printf("  client_find     = %d\n", br.TimesMicro["client_find"])
	fmt.Printf("  server_compute  = %d\n", br.TimesMicro["server_compute"])
	fmt.Printf("  client_recover  = %d\n", br.TimesMicro["client_recover"])
	fmt.Printf("  online_total    = %d\n", br.TimesMicro["online_total"])
	fmt.Printf("  end_to_end      = %d\n", br.TimesMicro["end_to_end"])
	fmt.Println("\nCommunication / storage estimates:")
	fmt.Printf("  setup_download_bytes  = %d (%.2f MB)\n", c.SetupDownloadBytes, mb(c.SetupDownloadBytes))
	fmt.Printf("  online_request_bytes  = %d (%.4f MB)\n", c.OnlineRequestBytes, mb(c.OnlineRequestBytes))
	fmt.Printf("  online_response_bytes = %d (%.4f MB)\n", c.OnlineResponseBytes, mb(c.OnlineResponseBytes))
	fmt.Printf("  online_total_bytes    = %d (%.4f MB)\n", c.OnlineTotalBytes, mb(c.OnlineTotalBytes))
	fmt.Printf("  server_local_read     = %d (%.4f MB)\n", c.ServerLocalReadBytes, mb(c.ServerLocalReadBytes))
	fmt.Printf("  client_hint_storage   = %d (%.2f MB)\n", c.ClientHintStorageBytes, mb(c.ClientHintStorageBytes))
	fmt.Printf("  replacement_storage   = %d (%.2f MB)\n", c.ReplacementStorageBytes, mb(c.ReplacementStorageBytes))
	fmt.Println("\nRuntime memory:")
	fmt.Printf("  before_setup HeapAlloc=%.2f MB, Sys=%.2f MB\n", mb(br.MemoryBefore.HeapAlloc), mb(br.MemoryBefore.Sys))
	fmt.Printf("  after_setup  HeapAlloc=%.2f MB, Sys=%.2f MB\n", mb(br.MemoryAfterSetup.HeapAlloc), mb(br.MemoryAfterSetup.Sys))
	fmt.Printf("  after_query  HeapAlloc=%.2f MB, Sys=%.2f MB\n", mb(br.MemoryAfterQuery.HeapAlloc), mb(br.MemoryAfterQuery.Sys))
	fmt.Println("==========================================================")
}

func main() {
	dbPath := flag.String("db", "", "GRID database file generated by Python, e.g., xxx.pir.db")
	metaPath := flag.String("meta", "", "metadata JSON generated by Python, e.g., xxx.pir.meta.json")
	indexFlag := flag.Int64("index", -1, "PIR index x; if omitted, use -qx/-qy or -query-json")
	qx := flag.Float64("qx", math.NaN(), "query x")
	qy := flag.Float64("qy", math.NaN(), "query y")
	queryJSON := flag.String("query-json", "", "optional query-index JSON generated by Python")
	seed := flag.Int64("seed", 1, "random seed")
	outJSON := flag.String("metrics-json", "", "optional path to save metrics as JSON")
	showIDs := flag.Bool("show-ids", false, "include skyline ids in output JSON and console")
	flag.Parse()

	if *dbPath == "" || *metaPath == "" {
		log.Fatalf("usage: -db <file.pir.db> -meta <file.pir.meta.json> [-index x | -qx qx -qy qy | -query-json q.json]")
	}

	endToEndStart := time.Now()
	meta, err := loadMeta(*metaPath)
	if err != nil {
		log.Fatalf("load meta failed: %v", err)
	}

	loadStart := time.Now()
	if err := loadFullDB(*dbPath, meta); err != nil {
		log.Fatalf("load DB failed: %v", err)
	}
	loadTime := time.Since(loadStart)
	padDBForPiano()

	var pirIndex uint64
	var gc, gr uint64
	if *queryJSON != "" {
		raw, err := os.ReadFile(*queryJSON)
		if err != nil {
			log.Fatalf("read query json failed: %v", err)
		}
		var q QueryIndex
		if err := json.Unmarshal(raw, &q); err != nil {
			log.Fatalf("parse query json failed: %v", err)
		}
		pirIndex = q.PIRIndex
		gc = q.GridCol
		gr = q.GridRow
	} else if *indexFlag >= 0 {
		pirIndex = uint64(*indexFlag)
		if pirIndex >= DBSize {
			fmt.Printf("query index %d out of range [0,%d)", pirIndex, DBSize)
			pirIndex = pirIndex % DBSize
			fmt.Printf(",new query index=%d", pirIndex)
		}
	} else {
		if math.IsNaN(*qx) || math.IsNaN(*qy) {
			log.Fatalf("provide either -index, -query-json, or both -qx and -qy")
		}
		gc, gr, pirIndex, err = indexFromQuery(meta, *qx, *qy)
		if err != nil {
			log.Fatalf("locate index from query failed: %v", err)
		}
	}

	runtime.GC()
	memBefore := memSnapshot()
	rng := rand.New(rand.NewSource(*seed))
	st, setupTime := setupPiano(rng)
	runtime.GC()
	memAfterSetup := memSnapshot()

	record, pt, err := queryPiano(st, pirIndex)
	if err != nil {
		log.Fatalf("Piano-PIR query failed: %v", err)
	}
	runtime.GC()
	memAfterQuery := memSnapshot()

	ids := decodeRecord(record)
	times := map[string]int64{
		"load_db":        micros(loadTime),
		"setup":          micros(setupTime),
		"client_find":    micros(pt.ClientFind),
		"server_compute": micros(pt.Server),
		"client_recover": micros(pt.ClientRecover),
		"online_total":   micros(pt.OnlineTotal),
		"end_to_end":     micros(time.Since(endToEndStart)),
	}
	if pt.EndToEnd > 0 {
		times["query_end_to_end"] = micros(pt.EndToEnd)
	}

	br := BenchResult{
		DBRecords:        DBSize,
		RecordWords:      RecordWords,
		ChunkSize:        ChunkSize,
		ChunkNum:         ChunkNum,
		M1:               st.M1,
		M2:               st.M2,
		PIRIndex:         pirIndex,
		GridCol:          gc,
		GridRow:          gr,
		SkylineCount:     len(ids),
		TimesMicro:       times,
		Communication:    estimateComm(st),
		MemoryBefore:     memBefore,
		MemoryAfterSetup: memAfterSetup,
		MemoryAfterQuery: memAfterQuery,
	}
	if *showIDs {
		br.SkylineIDs = ids
	}

	printSummary(br)
	if *showIDs {
		fmt.Printf("skyline_ids=%v\n", ids)
	}

	if *outJSON != "" {
		raw, err := json.MarshalIndent(br, "", "  ")
		if err != nil {
			log.Fatalf("marshal metrics failed: %v", err)
		}
		if err := os.WriteFile(*outJSON, raw, 0644); err != nil {
			log.Fatalf("write metrics json failed: %v", err)
		}
		fmt.Printf("metrics saved to %s\n", *outJSON)
	}
}
